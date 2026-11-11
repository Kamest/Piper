// A pane-local touch keyboard; no inferred answers or queued input.
import { h } from './dom.js';
import { markBidi } from './text.js';
const KEYS = [
    ['Escape', 'Esc', 'Escape'], ['Tab', 'Tab', 'Tab'], ['ArrowUp', '↑', 'Up arrow'], ['Space', 'Space', 'Space'], ['Type', 'Type', 'Show keyboard'],
    ['Shift', 'Shift', 'Shift, applies to next arrow or Tab'], ['ArrowLeft', '←', 'Left arrow'], ['ArrowDown', '↓', 'Down arrow'], ['ArrowRight', '→', 'Right arrow'], ['Enter', 'Enter', 'Enter'],
];
/** Phone dock only (Claude boards 2 / 3c): one scrolling row adds ^C, ⇧Tab (Claude's mode switch) and /. */
const DOCK_KEYS = [['CtrlC', '^C', 'Control C, interrupt'], ['ShiftTab', '⇧tab', 'Shift Tab'], ['Slash', '/', 'Slash']];
const DOCK_LABEL = { Escape: 'esc', Tab: 'tab', Enter: '⏎', Space: 'space', Shift: '⇧' };
/** Match xterm's normal/application cursor modes and xterm's Shift modifier encoding. */
export function terminalKey(key, shift, application) {
    const arrow = { ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D' }[key];
    if (arrow)
        return shift ? `\x1b[1;2${arrow}` : `\x1b${application ? 'O' : '['}${arrow}`;
    if (key === 'Tab')
        return shift ? '\x1b[Z' : '\t';
    if (key === 'ShiftTab')
        return '\x1b[Z';
    if (shift)
        return null;
    return { Escape: '\x1b', Space: ' ', Enter: '\r', CtrlC: '\x03', Slash: '/' }[key] ?? null;
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
    tools = new Map();
    picker = h('input', { type: 'file', accept: 'image/*', hidden: true, 'aria-label': 'Choose image' });
    sheet = h('div', { class: 'termtext', role: 'dialog', 'aria-modal': 'true', hidden: true });
    field = h('textarea', { spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', wrap: 'off' });
    sheetTitle = h('strong');
    sheetDetail = h('span', { class: 'mut small' });
    sheetStatus = h('p', { class: 'termtext-status', 'aria-live': 'polite' });
    sheetFoot = h('div', { class: 'termtext-foot' });
    warning = h('p', { class: 'termtext-warning' });
    selectionButton = h('button', { type: 'button' }, 'Copy selection');
    primaryButton = h('button', { type: 'button', class: 'primary' });
    doneButton = h('button', { type: 'button' });
    mode = '';
    epoch = 0;
    restoreTyping = false;
    uploading = false;
    message = '';
    extra;
    messageTimer = 0;
    selectionChanged = () => this.renderSheet();
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
                const button = h('button', { type: 'button', 'aria-label': name, 'data-key': key }, label);
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
        this.extra = h('div', { class: 'termkeys-extra', role: 'group', 'aria-label': 'More keys' });
        for (const [key, label, name] of DOCK_KEYS) {
            const button = h('button', { type: 'button', 'aria-label': name, 'data-key': key }, label);
            button.addEventListener('pointerdown', e => e.preventDefault());
            button.addEventListener('click', () => this.press(key));
            this.buttons.set(key, button);
            this.extra.append(button);
        }
        this.el.append(this.extra);
        const tools = h('div', { class: 'termkeys-tools', role: 'group', 'aria-label': 'Clipboard and image' });
        for (const [key, label, name] of [['copy', 'Copy…', 'Select and copy terminal text'],
            ['paste', 'Paste', 'Paste from clipboard'], ['image', 'Image…', 'Attach image']]) {
            const button = h('button', { type: 'button', 'aria-label': name, 'data-key': key }, label);
            // the dock keeps the keyboard (and the xterm focus) up across a tap, as the keys do
            button.addEventListener('pointerdown', e => { if (this.docked())
                e.preventDefault(); });
            button.addEventListener('click', () => {
                if (!this.visible || this.shift || document.hidden)
                    return;
                if (key === 'copy')
                    this.openSheet('copy');
                else if (this.state.connected && key === 'paste')
                    this.readClipboard();
                else if (this.state.connected && !this.uploading && key === 'image')
                    this.picker.click();
            });
            this.tools.set(key, button);
            tools.append(button);
        }
        this.el.append(tools, this.picker);
        this.picker.addEventListener('change', () => {
            const file = this.picker.files?.[0];
            this.picker.value = '';
            if (!file || !this.available() || !this.state.connected || this.uploading)
                return;
            if (!file.type.startsWith('image/')) {
                this.notify('Choose an image file');
                return;
            }
            const epoch = this.epoch;
            this.uploading = true;
            this.notify(`Uploading ${file.name}…`);
            void this.deps.upload(file).then(() => {
                if (epoch === this.epoch)
                    this.notify('Image inserted · Enter not pressed');
            }, () => { if (epoch === this.epoch)
                this.notify('Image upload failed · tap Image… to retry'); })
                .finally(() => { this.uploading = false; this.render(); });
        });
        const titleId = `terminal-text-title-${serial}`;
        this.sheetTitle.id = titleId;
        this.sheet.setAttribute('aria-labelledby', titleId);
        const head = h('div', { class: 'termtext-head' }, h('div', {}, this.sheetTitle, this.sheetDetail), this.doneButton);
        const actions = h('div', { class: 'termtext-actions' }, this.selectionButton, this.primaryButton);
        this.sheetFoot.append(this.sheetStatus, actions);
        this.sheet.append(head, this.field, this.warning, this.sheetFoot);
        pane?.append(this.sheet);
        this.sheet.addEventListener('pointerdown', e => { if (e.target !== this.field)
            e.preventDefault(); });
        this.doneButton.addEventListener('click', () => this.closeSheet());
        this.selectionButton.addEventListener('click', () => this.mode === 'copy' ? this.copy(false) : this.closeSheet());
        this.primaryButton.addEventListener('click', () => {
            if (this.mode === 'copy')
                this.copy(true);
            else if (this.canInsert() && this.field.value) {
                this.deps.paste(this.field.value);
                this.closeSheet(false);
                this.deps.keyboard(true);
            }
        });
        this.field.addEventListener('input', () => this.renderSheet());
        this.field.addEventListener('select', this.selectionChanged);
        document.addEventListener('selectionchange', this.selectionChanged);
        this.sheet.addEventListener('keydown', e => {
            e.stopPropagation();
            if (e.key === 'Escape') {
                e.preventDefault();
                this.closeSheet();
            }
            if (e.key === 'Tab') {
                const nodes = [this.doneButton, this.field, this.selectionButton, this.primaryButton].filter(n => !n.disabled);
                const index = nodes.indexOf(document.activeElement);
                const next = nodes[(index + (e.shiftKey ? -1 : 1) + nodes.length) % nodes.length];
                e.preventDefault();
                next?.focus({ preventScroll: true });
            }
        });
        this.media.addEventListener('change', this.redraw);
        document.addEventListener('visibilitychange', this.background);
    }
    update(state) {
        if (state.connected !== this.state.connected)
            this.epoch++;
        if (!state.alive || !state.active || state.connected !== this.state.connected)
            this.shift = false;
        this.state = state;
        this.render();
    }
    reset() { this.shift = false; this.message = ''; window.clearTimeout(this.messageTimer); this.render(); }
    canInsert() { return this.available() && this.state.connected; }
    notify(message) {
        this.message = message;
        window.clearTimeout(this.messageTimer);
        this.messageTimer = window.setTimeout(() => { this.message = ''; this.render(); }, 6000);
        this.render();
    }
    openSheet(mode, text = '', blocked = false) {
        this.epoch++;
        this.mode = mode;
        this.restoreTyping = this.deps.typing();
        this.sheet.hidden = false;
        this.field.readOnly = mode === 'copy';
        this.sheetTitle.textContent = mode === 'copy' ? 'Select text' : 'Paste text';
        this.doneButton.textContent = mode === 'copy' ? 'Done' : 'Cancel';
        this.field.setAttribute('aria-label', mode === 'copy' ? 'Terminal text' : 'Text to paste into terminal');
        this.field.placeholder = mode === 'paste' ? 'Paste or type here' : '';
        if (mode === 'copy') {
            this.sheetFoot.prepend(this.sheetStatus);
            const snapshot = this.deps.bufferText();
            this.field.value = snapshot.text;
            this.sheetDetail.textContent = markBidi(`Last ${snapshot.lines.toLocaleString()} lines of ${this.state.label} · read-only${snapshot.trimmed ? ' · limited' : ''}`);
            this.sheetStatus.textContent = 'Long-press to select. Copy selection or copy everything.';
        }
        else {
            this.sheet.insertBefore(this.sheetStatus, this.field);
            this.field.value = text;
            this.sheetDetail.textContent = markBidi(`Into ${this.state.label} · nothing is sent until Insert`);
            this.sheetStatus.textContent = blocked ? 'Clipboard access was blocked. Long-press the box and choose Paste.' : '';
        }
        this.sheetStatus.className = 'termtext-status';
        this.renderSheet();
        this.field.focus({ preventScroll: true });
        if (mode === 'copy')
            this.field.scrollTop = this.field.scrollHeight;
    }
    closeSheet(focus = true) {
        if (!this.mode)
            return;
        this.epoch++;
        this.mode = '';
        this.sheet.hidden = true;
        this.field.value = '';
        if (focus && this.available()) {
            if (this.restoreTyping && this.state.connected)
                this.deps.keyboard(true);
            else
                [...this.toggles].find(button => button.isConnected && !button.hidden)?.focus({ preventScroll: true });
        }
    }
    readClipboard() {
        // Invoke the API inside the click. Open the native input in the same gesture so Safari can focus it.
        let read;
        try {
            read = navigator.clipboard?.readText ? navigator.clipboard.readText() : Promise.reject();
        }
        catch {
            read = Promise.reject();
        }
        this.openSheet('paste');
        const epoch = this.epoch;
        void read.then(text => {
            if (epoch !== this.epoch || this.mode !== 'paste' || !this.canInsert())
                return;
            if (!text) {
                this.closeSheet();
                this.notify('Clipboard is empty');
                return;
            }
            if (/[\r\n]/.test(text) && !this.deps.bracketed()) {
                this.field.value = text;
                this.renderSheet();
                return;
            }
            this.deps.paste(text);
            this.closeSheet();
            this.notify(`Pasted ${text.length} characters · Enter not pressed`);
        }, () => {
            if (epoch === this.epoch && this.mode === 'paste')
                this.sheetStatus.textContent = 'Clipboard access was blocked. Long-press the box and choose Paste.';
        });
    }
    copy(all) {
        if (this.mode !== 'copy')
            return;
        const start = all ? 0 : this.field.selectionStart, end = all ? this.field.value.length : this.field.selectionEnd;
        if (start === end)
            return;
        const text = this.field.value.slice(start, end), epoch = this.epoch;
        const done = () => { if (epoch === this.epoch) {
            this.sheetStatus.textContent = markBidi(`✓ Copied ${text.split('\n').length} lines`);
            this.sheetStatus.className = 'termtext-status ok';
        } };
        const fallback = () => {
            if (epoch !== this.epoch)
                return;
            this.field.focus({ preventScroll: true });
            this.field.setSelectionRange(start, end);
            try {
                if (document.execCommand?.('copy')) {
                    done();
                    return;
                }
            }
            catch { /* native selection remains */ }
            this.sheetStatus.textContent = 'Clipboard blocked by the browser. The text is selected; long-press it and choose Copy.';
            this.sheetStatus.className = 'termtext-status warn';
            this.renderSheet();
        };
        try {
            if (navigator.clipboard?.writeText)
                void navigator.clipboard.writeText(text).then(done, fallback);
            else
                fallback();
        }
        catch {
            fallback();
        }
    }
    renderSheet() {
        if (!this.mode)
            return;
        const copy = this.mode === 'copy';
        this.selectionButton.textContent = copy ? 'Copy selection' : 'Cancel';
        this.primaryButton.textContent = copy ? 'Copy all' : 'Insert';
        this.selectionButton.disabled = copy && this.field.selectionStart === this.field.selectionEnd;
        this.primaryButton.disabled = !this.field.value || (!copy && !this.canInsert());
        const breaks = (this.field.value.match(/\r\n|\r|\n/g) ?? []).length;
        this.warning.hidden = copy || !breaks || this.deps.bracketed();
        this.warning.textContent = markBidi(`Contains ${breaks} line break${breaks === 1 ? '' : 's'}. Without bracketed paste the program may run each line as it arrives.`);
        if (!copy && !this.state.connected)
            this.sheetStatus.textContent = 'Disconnected · nothing was sent';
    }
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
    docked() { return !!this.deps.dock?.(); }
    available() {
        return this.paneWidth >= 252 && (this.media.matches || this.docked()) && this.state.alive && this.state.active && !document.hidden;
    }
    press(key) {
        if (!this.visible || !this.state.connected || document.hidden)
            return;
        this.message = '';
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
        if (!available) {
            this.opened = false;
            this.closeSheet(false);
        }
        const docked = this.docked();
        const visible = available && (this.opened || docked);
        if (!visible) {
            this.shift = false;
            this.closeSheet(false);
        }
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
        this.el.classList.toggle('dock', docked);
        this.extra.hidden = !docked;
        this.el.classList.toggle('disconnected', !connected);
        this.el.classList.toggle('shifted', this.shift);
        this.el.setAttribute('aria-label', markBidi(`Terminal keys for ${this.state.label}`));
        this.keyboardStatus.textContent = connected && typing ? 'Keyboard open' : '';
        this.status.classList.toggle('idle', connected && !this.shift);
        this.status.setAttribute('aria-hidden', String(connected && !this.shift));
        this.status.textContent = this.shift ? 'Shift on · next arrow or Tab' : 'Terminal keys';
        if (this.message && connected && !this.shift) {
            this.status.textContent = markBidi(this.message);
            this.status.classList.remove('idle');
            this.status.setAttribute('aria-hidden', 'false');
        }
        if (!connected) {
            this.status.textContent = '';
            this.status.append(h('span', { class: 'termkeys-disconnected' }, 'Disconnected'), h('span', { class: 'termkeys-detail' }, 'Keys off until reconnected'));
        }
        for (const [key, plain, name] of [...KEYS, ...DOCK_KEYS]) {
            const button = this.buttons.get(key);
            const label = docked ? DOCK_LABEL[key] ?? plain : plain;
            const modified = this.shift && (key === 'Tab' || key.startsWith('Arrow'));
            const disabled = !connected || (this.shift && ['Escape', 'Space', 'Enter', 'CtrlC', 'Slash'].includes(key));
            button.setAttribute('aria-disabled', String(disabled));
            button.textContent = markBidi(key === 'Type' ? typing ? 'Hide' : 'Type' : key === 'Shift' && this.shift ? docked ? '⇧ on' : '⇧ Shift' : `${modified ? '⇧' : ''}${label}`);
            button.setAttribute('aria-label', markBidi(key === 'Type' ? typing ? 'Hide keyboard' : 'Show keyboard' : `${modified ? 'Shift ' : ''}${name}`));
            if (key === 'Shift' || key === 'Type') {
                const pressed = key === 'Shift' ? this.shift : typing;
                button.setAttribute('aria-pressed', String(pressed));
                button.classList.toggle('on', pressed);
            }
        }
        for (const [key, button] of this.tools) {
            button.setAttribute('aria-disabled', String(this.shift || (key !== 'copy' && !connected) || (key === 'image' && this.uploading)));
            if (key === 'image')
                button.textContent = this.uploading ? 'Uploading…' : 'Image…';
        }
        this.renderSheet();
    }
    dispose() {
        this.update({ ...this.state, active: false });
        this.paneObserver?.disconnect();
        window.cancelAnimationFrame(this.paneFrame);
        this.media.removeEventListener('change', this.redraw);
        document.removeEventListener('visibilitychange', this.background);
        document.removeEventListener('selectionchange', this.selectionChanged);
        window.clearTimeout(this.messageTimer);
        this.epoch++;
        for (const button of this.toggles)
            button.remove();
        this.toggles.clear();
        this.el.remove();
        this.sheet.remove();
    }
}
//# sourceMappingURL=termkeys.js.map