// One terminal = one xterm.js instance + one WebSocket to piperd /ws/<id>.
//
// The socket authenticates with its first message ({"auth": token}); output arrives as
// binary frames, starting with the replay from ptyhost's latest safe point, so a reload
// or a second window rebuilds the screen. Keys are the terminal's: Esc and Ctrl+K go to
// Claude Code (no global handler may take them), Shift/Option+Enter send ESC CR (newline
// in Claude Code), Option stays Option (Czech keyboard needs it for @ # $ { } [ ] | ~).
//
// while a native terminal is attached (`piper attach`), that terminal sizes the PTY:
// ptyhost ignores this view's resize, so it holds the PTY's size (no fit) and scrolls; a pane
// too small for it offers the Conversation instead. Typing here keeps working.
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import { Unicode11Addon } from '@xterm/addon-unicode11';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { SearchAddon } from '@xterm/addon-search';
import { terminalQuery, reference, sshOnly } from './environment.js';
import { call, token } from './api.js';
import { terminalSocket, terminalStreamVisibleOnly, onTransportReady } from './transport.js';
import { TERM_KEEPS, appKey, appMod, clipAction, hostKey, scratchKey, isMac, lineKey, osc52Text } from './clipkeys.js';
import { terminalTheme } from './theme.js';
import { piperUrl, terminalReferences } from './references.js';
import { markBidi } from './text.js';
import { TerminalKeys } from './termkeys.js';
/** A bounded local snapshot for native touch selection; soft wraps stay one logical line. */
export function terminalBufferText(buffer) {
    const parts = [], encoder = new TextEncoder();
    let bytes = 0, lines = 0, wrapped = false, trimmed = false;
    for (let row = buffer.length - 1; row >= 0; row--) {
        const line = buffer.getLine(row);
        if (!line)
            continue;
        const text = line.translateToString(!wrapped);
        if (!parts.length && !text.trim())
            continue;
        if (parts.length && !wrapped && lines >= 2000) {
            trimmed = true;
            break;
        }
        if (!parts.length || !wrapped)
            lines++;
        const encoded = encoder.encode(text + (parts.length && !wrapped ? '\n' : ''));
        const remaining = 256 * 1024 - bytes;
        if (encoded.length > remaining) {
            let start = encoded.length - remaining;
            while (start < encoded.length && (encoded[start] & 0xc0) === 0x80)
                start++;
            parts.unshift(new TextDecoder().decode(encoded.subarray(start)));
            trimmed = true;
            break;
        }
        parts.unshift(new TextDecoder().decode(encoded));
        bytes += encoded.length;
        wrapped = line.isWrapped;
    }
    return { text: parts.join(''), lines, trimmed };
}
/** opt-in behaviour (policy, via the GUI state - main.ts sets it on every poll) */
export const TERM_FLAGS = { hostLinks: false, armEnter: false, dbSide: false, clickFiles: false, nav: false };
/** Host mode opens the selected browser outside a Safari Dock app; failures stay visible. */
export function openUrl(uri, refused, internal) {
    if (internal?.(uri))
        return;
    if (piperUrl(uri, location.href || `${location.protocol}//${location.host}`, window.__PIPER__?.origins, terminalStreamVisibleOnly())) {
        refused('Open this link inside Piper');
        return;
    }
    if (!/^https?:\/\//i.test(uri))
        return;
    if (!TERM_FLAGS.hostLinks || terminalStreamVisibleOnly()) {
        window.open(uri, '_blank', 'noopener');
        return;
    }
    call('POST', '/api/open-url', { url: uri }).catch(() => refused('Could not open the host browser. Check Piper’s browser setting and that the browser is installed.'));
}
// `src/main/java/Foo.java:123` (codenav FILE_LINK), relative to the session's project; the
// sandbox mounts the project at its host path, so the path is the IntelliJ path too
/** ⌘ shortcuts the GUI owns (main.ts globalKeys); every other key belongs to the terminal */
export const APP_KEYS = ['k', 'i', 'f', 'p', 'j', 'b', 'e', 'g', 'o', 'l', '[', '\\', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'];
const MAC = isMac(navigator.platform || navigator.userAgent);
let FONT = 13.5;
/** Settings is an enum; malformed or old policy values retain the original terminal size. */
export function terminalFontSize(value) {
    return ['12', '13.5', '15', '16', '18', '20'].includes(String(value)) ? Number(value) : 13.5;
}
/** below this a terminal shrunk to fit its attached terminal's size is not readable: the card asks */
const FONT_READABLE = 9;
const FONT_MIN = 6;
/** how long a mouse release waits for Claude Code's OSC 52 before its clipboard write is dropped */
const COPY_WAIT_MS = 1500;
/** a foreign resize (another window or device) is answered once, then this view yields until the next focus */
const REASSERT_MS = 10_000;
/** 9.55: the PTY has one size and the last GUI window to send one wins (another tab, a second window, a
 *  phone). Coming back to this window - focus, visible again, back from the bfcache - takes it back. */
const LIVE = new Set();
let reclaimWired = false;
function wireReclaim() {
    if (reclaimWired)
        return;
    reclaimWired = true;
    const reclaim = () => { for (const v of LIVE)
        v.reclaim(); };
    // 9.155: Web Inspector console - every open terminal's input path, to run while one ignores keys or the wheel
    window.__piperTermDiag = () => [...LIVE].map((v) => v.diag());
    window.addEventListener('focus', reclaim);
    window.addEventListener('pageshow', reclaim);
    document.addEventListener('visibilitychange', reclaim);
}
const describe = (el) => el ? `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${String(el.className || '').split(' ').slice(0, 2).join('.')}`.slice(0, 60) : 'none';
/** 9.155: ⋯ Copy terminal diagnostics - __piperTermDiag() without a Web Inspector (the Dock app has no Develop
 *  menu). This session's terminal first, then every other open one; ids, sizes, states and timestamps only. */
export function termDiagText(sid, views, now = new Date()) {
    const all = [...views].map((v) => v.diag());
    all.sort((a, b) => Number(b.id === sid) - Number(a.id === sid));
    const standalone = typeof matchMedia === 'function' && matchMedia('(display-mode: standalone)').matches;
    return JSON.stringify({ at: now.toISOString(), session: sid, ua: navigator.userAgent, standalone, terminals: all }, null, 2);
}
export class TermView {
    id;
    cwd;
    ev;
    el;
    /** pane bar (title, state), shown in the grid only */
    label;
    host;
    term;
    fit = new FitAddon();
    search = new SearchAddon();
    ws = null;
    closed = false;
    retry = 0;
    retryTimer = 0;
    stopReady;
    fitTimer = 0;
    resizeObserver = null;
    releaseMouse;
    frozen = false;
    /** the PTY's size as far as this view knows (what it sent, or a foreign `size`): a fit sends only a difference */
    lastSize = '';
    reassertAt = 0;
    claimNext = false; // 9.155: the next resize sent is a Redraw's claim (piperd echoes it with claim)
    refusedAt = 0;
    /** between connect and the settled replay: no fit (it would redraw the replay at another size) */
    replaying = false;
    /** Offset is committed only after xterm parsed it; queued output survives a suspended stream. */
    checkpoint = null;
    screenGeneration = 0;
    streamCycle = 0;
    connectingFlush = false;
    processIdentity = '';
    selectionReplay = null;
    /** fills the clipboard write opened by the last mouse release (armCopy) */
    pendingCopy = null;
    /** TUI-owned selection is not in xterm's selection manager; keep it for an explicit ⌘C. */
    copiedSelection = '';
    /** a native terminal is attached and sizes the PTY (ptyhost's count, 0/1) */
    tty = 0;
    pty = { cols: 0, rows: 0 };
    /** the card over a pane too small for the attached terminal's size, until "Show here" */
    ttyCard;
    ttyBar;
    cardDismissed = false;
    fontKey = '';
    fontTries = 0;
    fontTimer = 0;
    touchKeys = null;
    touchInputVersion = 0;
    touchConnected = false;
    inputState = { alive: false, active: false, label: '', visible: false };
    processExited = false;
    channelClosed = false;
    channelReconnect = false;
    channelBanner;
    channelText;
    channelDetail;
    channelButton;
    /** 9.155: when a key / wheel reached this view, xterm produced input, a write left; what was dropped and why */
    input = { keyAt: 0, wheelAt: 0, dataAt: 0, sentAt: 0, dropped: 0, dropAt: 0, dropWhy: '', lastKey: '' };
    /** A retained terminal/replay is not evidence of a live session. Main/Host supply process lifetime explicitly. */
    setInputState(alive, active, label, visible = active) {
        if (alive !== this.inputState.alive || active !== this.inputState.active)
            this.touchInputVersion++;
        this.inputState = { alive, active, label, visible };
        if (!this.touchKeys && typeof window.matchMedia === 'function') {
            this.touchKeys = new TerminalKeys({
                send: data => {
                    if (!this.inputState.alive || !this.inputState.active || this.processExited || this.closed || this.ws?.readyState !== WebSocket.OPEN)
                        return;
                    this.clearCopy();
                    if (TERM_FLAGS.armEnter && data === '\r')
                        this.armCopy();
                    this.term.input(data, true);
                },
                application: () => !!this.term.modes.applicationCursorKeysMode,
                typing: () => !!this.term.textarea && document.activeElement === this.term.textarea,
                keyboard: show => { if (show)
                    this.term.focus();
                else
                    this.term.textarea?.blur(); },
                bufferText: () => terminalBufferText(this.term.buffer.active),
                bracketed: () => this.term.modes.bracketedPasteMode,
                paste: text => { if (this.touchInputReady()) {
                    this.clearCopy();
                    this.term.paste(text);
                } },
                upload: file => this.image(file, true),
            }, this.host.parentElement);
            this.host.parentElement.append(this.touchKeys.el);
        }
        this.syncStreaming();
        this.syncKeys();
    }
    syncKeys() {
        const connected = this.ws?.readyState === WebSocket.OPEN && (!terminalStreamVisibleOnly() || !this.replaying);
        if (connected !== this.touchConnected) {
            this.touchConnected = connected;
            this.touchInputVersion++;
        }
        this.touchKeys?.update({ ...this.inputState, alive: this.inputState.alive && !this.closed && !this.processExited && !this.channelClosed,
            connected });
        if (terminalStreamVisibleOnly())
            this.term.options.disableStdin = this.replaying || this.ws?.readyState !== WebSocket.OPEN || !this.inputState.alive || this.processExited;
    }
    /** A fixed header control; the panel itself stays in this terminal's clipping surface. */
    keysToggle() { return this.touchKeys?.createToggle() ?? null; }
    constructor(id, cwd, ev) {
        this.id = id;
        this.cwd = cwd;
        this.ev = ev;
        this.el = document.createElement('div');
        this.el.className = 'term';
        this.label = document.createElement('div');
        this.label.className = 'tlabel';
        this.host = document.createElement('div');
        this.host.className = 'thost';
        this.ttyBar = document.createElement('div');
        this.ttyBar.className = 'ttybar hidden';
        this.ttyCard = document.createElement('div');
        this.ttyCard.className = 'ttycard hidden';
        // the card covers the terminal only: a grid pane's bar (◀ ▶ ✕) stays usable
        const wrap = document.createElement('div');
        wrap.className = 'thostwrap';
        this.channelBanner = document.createElement('div');
        this.channelBanner.className = 'terminal-channel hidden';
        this.channelBanner.setAttribute('role', 'alert');
        const channelCopy = document.createElement('div');
        channelCopy.className = 'terminal-channel-copy';
        this.channelText = document.createElement('span');
        this.channelDetail = document.createElement('span');
        this.channelDetail.className = 'mut small';
        channelCopy.append(this.channelText, this.channelDetail);
        this.channelButton = document.createElement('button');
        this.channelButton.textContent = 'Reconnect terminal';
        this.channelButton.addEventListener('click', () => {
            if (this.channelReconnect || this.closed)
                return;
            this.pauseStream();
            this.channelClosed = false;
            this.channelReconnect = true;
            this.channelText.textContent = 'Reconnecting terminal…';
            this.channelDetail.textContent = 'Input is not resent.';
            this.channelButton.textContent = 'Reconnecting…';
            this.channelButton.disabled = true;
            this.connect();
        });
        this.channelBanner.append(channelCopy, this.channelButton);
        wrap.append(this.host, this.ttyCard, this.channelBanner);
        this.el.append(this.label, this.ttyBar, wrap);
        this.term = new Terminal({
            fontFamily: '"JetBrains Mono", "JetBrainsMono Nerd Font Mono", "JetBrainsMono NFM", "SF Mono", Menlo, monospace',
            fontSize: FONT,
            lineHeight: 1.3,
            cursorBlink: false,
            cursorStyle: 'bar',
            scrollback: 10000,
            macOptionIsMeta: false,
            // Claude Code turns on mouse reporting: Option-drag selects on macOS, Shift-drag elsewhere
            macOptionClickForcesSelection: true,
            allowProposedApi: true,
            theme: terminalTheme(),
            // OSC 8 hyperlinks (Claude Code prints URLs this way): xterm's default handler confirm()s and
            // opens a blank window first, which a Safari web app (Add to Dock) blocks or keeps in-app
        });
        // Resolve the current policy on every click, including terminals created before a settings change.
        this.term.options.linkHandler = { activate: (_e, uri) => openUrl(uri, this.ev.onRefused, this.ev.onUrl) };
        this.term.loadAddon(this.fit);
        this.term.loadAddon(this.search);
        // Prefer the complete TUI-wrapped target over WebLinksAddon's single hard-line URL prefix.
        this.term.registerLinkProvider({ provideLinks: (y, cb) => cb(this.fileLinks(y)) });
        this.term.loadAddon(new WebLinksAddon((_e, uri) => openUrl(uri, this.ev.onRefused, this.ev.onUrl)));
        const uni = new Unicode11Addon();
        this.term.loadAddon(uni);
        this.term.unicode.activeVersion = '11';
        this.term.attachCustomKeyEventHandler((e) => this.key(e));
        // Claude Code copies its own (mouse) selection with OSC 52; a replay after a reload must
        // not overwrite the clipboard with whatever was copied back then
        this.term.parser.registerOscHandler(52, (data) => {
            const text = this.replaying ? null : osc52Text(data);
            if (text)
                this.copyOsc52(text);
            return true;
        });
        // Safari writes the clipboard only inside a user gesture, and the OSC 52 arrives later over
        // the socket: the mouse release opens a write that the OSC 52 following it fills in
        // a drag that ends outside the terminal releases on the window, not on the host
        this.host.addEventListener('mouseup', () => this.armCopy(), true);
        // policy clipboard_arm_enter: a drag that ends outside the terminal arms the write too
        let pressed = false;
        this.host.addEventListener('mousedown', () => {
            this.clearCopy();
            pressed = true;
        }, true);
        this.releaseMouse = (e) => {
            if (pressed && TERM_FLAGS.armEnter && !this.host.contains(e.target))
                this.armCopy();
            pressed = false;
        };
        window.addEventListener('mouseup', this.releaseMouse, true);
        this.wireTouchScroll();
        // 9.155: what reached the view at all, before xterm decides anything (__piperTermDiag)
        this.host.addEventListener('wheel', () => { this.input.wheelAt = Date.now(); }, { capture: true, passive: true });
        this.host.addEventListener('keydown', (e) => {
            this.input.keyAt = Date.now();
            // never the key itself for a printable one: only its kind (letter / digit / other) and where it went
            const k = e.key ?? '';
            const key = k.length === 1 ? (/\p{L}/u.test(k) ? 'letter' : /\d/.test(k) ? 'digit' : 'char') : k;
            const base = `${key} keyCode=${e.keyCode} composing=${e.isComposing} target=${describe(e.target)} active=${describe(document.activeElement)}`;
            this.input.lastKey = base;
            window.setTimeout(() => { if (this.input.lastKey === base)
                this.input.lastKey = `${base} prevented=${e.defaultPrevented}`; }, 0);
        }, { capture: true, passive: true });
        this.term.onData((d) => { this.input.dataAt = Date.now(); this.send({ t: 'in', d }); });
        this.term.onBinary((d) => { this.input.dataAt = Date.now(); this.sendBinary(d); });
        this.stopReady = onTransportReady(() => {
            if (this.closed || !this.wantsStream())
                return;
            window.clearTimeout(this.retryTimer);
            this.retryTimer = 0;
            this.retry = 0;
            if (!this.ws || this.ws.readyState > WebSocket.OPEN) {
                this.pauseStream();
                this.connect();
            }
        });
        LIVE.add(this);
        wireReclaim();
    }
    /** The DOM renderer draws block elements from the font, not as custom glyphs: with a line
     * height above 1 every row of Claude Code's logo (and any box art) gets a gap */
    domRenderer() {
        this.term.options.lineHeight = 1;
        this.scheduleFit();
    }
    /** once: attach to the (stable) body and open the socket */
    mount(parent) {
        if (!this.el.isConnected)
            parent.append(this.el);
        if (!this.term.element) {
            this.term.open(this.host);
            this.term.textarea?.addEventListener('focus', () => { this.ev.onFocus?.(); this.syncKeys(); });
            this.term.textarea?.addEventListener('blur', () => this.syncKeys());
            try {
                const gl = new WebglAddon();
                gl.onContextLoss(() => {
                    gl.dispose();
                    this.domRenderer();
                });
                this.term.loadAddon(gl);
            }
            catch {
                this.domRenderer(); // no WebGL (hardware acceleration off)
            }
            this.resizeObserver = new ResizeObserver(() => this.scheduleFit());
            this.resizeObserver.observe(this.host);
            // "Just look" at a terminal larger than the pane: the wheel pans the pane while it can
            // (xterm would take every wheel event for its scrollback or Claude Code's mouse mode)
            this.host.addEventListener('wheel', (e) => {
                if (!this.host.classList.contains('overflow'))
                    return;
                const h = this.host;
                const dx = e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX;
                const dy = e.shiftKey && !e.deltaX ? 0 : e.deltaY;
                const canX = (dx < 0 && h.scrollLeft > 0) || (dx > 0 && h.scrollLeft + h.clientWidth < h.scrollWidth);
                const canY = (dy < 0 && h.scrollTop > 0) || (dy > 0 && h.scrollTop + h.clientHeight < h.scrollHeight);
                if (!canX && !canY)
                    return; // at the edge: xterm scrolls its own history
                h.scrollBy(canX ? dx : 0, canY ? dy : 0);
                e.preventDefault();
                e.stopPropagation();
            }, { capture: true, passive: false });
            this.el.addEventListener('dragover', (e) => {
                if (e.dataTransfer?.types.includes('Files'))
                    e.preventDefault();
            });
            this.el.addEventListener('drop', (e) => {
                if (e.target?.closest?.('.termtext'))
                    return;
                const f = e.dataTransfer?.files[0];
                if (f && f.type.startsWith('image/')) {
                    e.preventDefault();
                    void this.image(f);
                }
            });
            // capture: xterm's own paste handler would send the (empty) text first
            this.el.addEventListener('paste', (e) => {
                if (e.target?.closest?.('.termtext'))
                    return;
                this.clearCopy();
                const item = [...(e.clipboardData?.items ?? [])].find((i) => i.type.startsWith('image/'));
                const f = item?.getAsFile();
                if (f) {
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    void this.image(f);
                }
            }, true);
            this.connect();
        }
        this.scheduleFit();
    }
    focus() {
        // Navigation is for reading on phones; tapping xterm itself still opens the keyboard.
        if (!window.matchMedia?.('(pointer: coarse)').matches)
            this.term.focus();
    }
    /** Route a finger drag through xterm's wheel path: scrollback in a shell, mouse
     * reports in a TUI (its alternate buffer has no browser scrollback to pan). */
    wireTouchScroll() {
        let remainder = 0;
        let touch = null;
        this.host.addEventListener('touchstart', (e) => {
            remainder = 0;
            touch = e.touches.length === 1 ? { id: e.touches[0].identifier,
                x: e.touches[0].clientX, y: e.touches[0].clientY, moved: false } : null;
            // xterm's document gesture handler otherwise consumes the same drag a second time.
            e.stopPropagation();
        }, { passive: true });
        this.host.addEventListener('touchmove', (e) => {
            e.stopPropagation();
            if (!touch || e.touches.length !== 1) {
                touch = null;
                return;
            }
            const point = e.touches[0];
            if (point.identifier !== touch.id)
                return;
            const dy = touch.y - point.clientY;
            const dx = touch.x - point.clientX;
            if (!touch.moved && Math.hypot(dx, dy) < 8)
                return;
            touch.moved = true;
            touch.x = point.clientX;
            touch.y = point.clientY;
            e.preventDefault();
            // Dispatch on the screen, where xterm installs mouse reporting, then bubble
            // through the host's existing attached-terminal overflow handler.
            const screen = this.host.querySelector('.xterm-screen');
            if (!this.host.classList.contains('overflow')) {
                const rowHeight = (screen?.clientHeight || this.host.clientHeight) / this.term.rows;
                if (rowHeight > 0) {
                    // A phone has little travel room. Preserve fractional rows across move events
                    // and emit every TUI wheel step (xterm reports only one per wheel event).
                    remainder += dy * 3;
                    const lines = Math.trunc(remainder / rowHeight);
                    remainder -= lines * rowHeight;
                    if (this.term.modes.mouseTrackingMode === 'none' && this.term.buffer.active.type === 'normal') {
                        if (lines)
                            this.term.scrollLines(lines);
                    }
                    else {
                        for (let i = 0; i < Math.abs(lines); i++) {
                            screen?.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true,
                                deltaMode: 1, deltaY: Math.sign(lines), clientX: point.clientX, clientY: point.clientY }));
                        }
                    }
                }
                return;
            }
            screen?.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true,
                deltaX: dx, deltaY: dy, clientX: point.clientX, clientY: point.clientY }));
        }, { passive: false });
        const end = (e) => {
            e.stopPropagation();
            if (touch?.moved)
                e.preventDefault(); // no synthetic click / keyboard after scrolling
            // A tap on a path or URL opens it: xterm's hover-driven links never fire for a finger, the tap only
            // focused the input and raised the keyboard.
            else if (touch && e.type === 'touchend' && this.tapLink(touch.x, touch.y))
                e.preventDefault();
            touch = null;
        };
        this.host.addEventListener('touchend', end, { passive: false });
        this.host.addEventListener('touchcancel', end, { passive: false });
    }
    /** Activate the link under a finger tap at (x, y) client coordinates; true when one was opened. */
    tapLink(x, y) {
        const screen = this.host.querySelector('.xterm-screen');
        if (!screen)
            return false;
        const r = screen.getBoundingClientRect();
        if (x < r.left || x >= r.right || y < r.top || y >= r.bottom)
            return false;
        const col = Math.floor((x - r.left) / (r.width / this.term.cols)) + 1;
        const row = this.term.buffer.active.viewportY + Math.floor((y - r.top) / (r.height / this.term.rows)) + 1;
        const link = this.fileLinks(row).find(({ range: { start, end } }) => (start.y < row || start.x <= col) && (end.y > row || end.x >= col));
        if (!link)
            return false;
        link.activate(new MouseEvent('click', { clientX: x, clientY: y }), link.text);
        return true;
    }
    /** type text into the prompt as a paste (no Enter): file references from the browser */
    insert(text) {
        this.clearCopy();
        this.term.paste(text);
        this.term.focus();
    }
    /** after becoming visible again: re-measure (a hidden element has no size) */
    refit() {
        this.scheduleFit();
    }
    /** 9.55: this window is the user's again: fit, which sends the size only if the PTY has another one */
    reclaim() {
        if (this.closed)
            return;
        this.syncStreaming();
        if (document.hidden)
            return;
        this.reassertAt = 0;
        this.scheduleFit();
    }
    /** during rail/inspector animations: no SIGWINCH storm (Claude Code redraws on each) */
    freeze(on) {
        this.frozen = on;
        if (!on)
            this.scheduleFit();
    }
    /** 9.155 ⋯ Redraw terminal: the path a foreign `size` takes, on demand - it brought two stuck Safari views
     *  back (2026-11-01, cause unknown), and it takes the PTY back from a phone that left it small. xterm's grid
     *  is bounced so it measures and paints again, then the fit sends this pane's size even when unchanged
     *  (ptyhost signals SIGWINCH on every resize, Claude Code redraws). A dead socket reconnects. */
    redraw() {
        if (this.closed)
            return;
        this.frozen = false;
        if (!this.ws || this.ws.readyState > WebSocket.OPEN) {
            window.clearTimeout(this.retryTimer);
            this.retryTimer = 0;
            this.retry = 0;
            this.pauseStream();
            this.connect();
        }
        const { cols, rows } = this.term;
        // only a shown view fits back right away; a hidden one keeps its grid until it is shown
        if (!this.tty && rows > 3 && this.el.offsetParent !== null) {
            // One ROW shorter first: Claude Code (Node) redraws only on a real size change, and its redraw sends the
            // mouse / paste modes again that a replay from a later safe point leaves out (2026-11-01). Never one column
            // narrower: its full-width rules then wrap onto the next line and the redraw lands one line off.
            this.term.resize(cols, rows - 1);
            this.send({ t: 'resize', c: cols, r: rows - 1, claim: true });
        }
        // A dead-key composition whose compositionend never came makes xterm swallow every key: end it.
        const ta = this.term.textarea;
        const helper = this.core()?._compositionHelper;
        if (helper) {
            helper._isComposing = false;
            helper._isSendingComposition = false;
        }
        if (ta) {
            ta.blur();
            ta.value = '';
        }
        this.fontKey = '';
        this.lastSize = '';
        this.reassertAt = Date.now(); // our own size now: a foreign echo of it is not answered again
        this.claimNext = true; // other windows (a phone open at the same time) follow it instead of answering once
        this.term.refresh(0, this.term.rows - 1);
        this.scheduleFit();
        this.term.focus();
    }
    /** 9.155: the input path's state for `__piperTermDiag()` */
    diag() {
        const r = this.host.getBoundingClientRect?.();
        const top = r && r.width > 0 ? document.elementFromPoint?.(r.left + r.width / 2, r.top + r.height / 2) : null;
        const cell = this.term
            ._core?._renderService?.dimensions?.css?.cell;
        return {
            id: this.id,
            socket: this.ws ? socketState(this.ws.readyState) : 'none',
            retrying: !!this.retryTimer,
            closed: this.closed, frozen: this.frozen, replaying: this.replaying, exited: this.processExited, tty: this.tty,
            visible: this.el.isConnected && this.el.offsetParent !== null,
            focused: !!this.term.textarea && document.activeElement === this.term.textarea,
            stdinDisabled: !!this.term.options.disableStdin,
            top: top ? `${top.tagName.toLowerCase()}.${String(top.className || '')}`.slice(0, 80) + (this.host.contains(top) ? ' (terminal)' : ' (OVER the terminal)') : '',
            grid: `${this.term.cols}x${this.term.rows}`, pty: `${this.pty.cols}x${this.pty.rows}`, lastSize: this.lastSize,
            cell: cell ? `${cell.width}x${cell.height}` : '?',
            mouse: String(this.term.modes?.mouseTrackingMode ?? '?'),
            buffer: String(this.term.buffer?.active?.type ?? '?'),
            ...this.input,
            composing: !!this.core()?._compositionHelper?._isComposing,
            textareaLength: this.term.textarea?.value.length ?? 0,
        };
    }
    core() {
        return this.term._core;
    }
    retheme() {
        this.term.options.theme = terminalTheme();
    }
    /** N1: a theme change recolours every open terminal (main, grid, host shells) */
    static rethemeAll() {
        for (const t of LIVE)
            t.retheme();
    }
    /** A saved preference refits every terminal, including hidden views when they become visible. */
    static setFontSizeAll(value) {
        const wanted = terminalFontSize(value);
        if (wanted === FONT)
            return;
        FONT = wanted;
        for (const view of LIVE) {
            view.term.options.fontSize = FONT;
            view.fontKey = '';
            view.fontTries = 0;
            window.clearTimeout(view.fontTimer);
            view.scheduleFit();
        }
    }
    dispose() {
        if (this.closed)
            return;
        this.closed = true;
        window.removeEventListener('mouseup', this.releaseMouse, true);
        this.resizeObserver?.disconnect();
        this.resizeObserver = null;
        window.clearTimeout(this.fitTimer);
        this.fitTimer = 0;
        window.clearTimeout(this.fontTimer);
        this.fontTimer = 0;
        this.stopReady();
        this.pauseStream();
        this.touchKeys?.dispose();
        LIVE.delete(this);
        this.clearCopy();
        this.ws?.close();
        this.term.dispose();
        this.el.remove();
    }
    touchInputReady() {
        return this.inputState.alive && this.inputState.active && !this.closed && !this.processExited && !this.channelClosed
            && this.ws?.readyState === WebSocket.OPEN && (!terminalStreamVisibleOnly() || !this.replaying);
    }
    async image(f, touch = false) {
        const version = this.touchInputVersion;
        try {
            if (touch && !this.touchInputReady())
                throw new Error('Terminal disconnected · nothing was sent');
            const path = await this.ev.onImage(f);
            if (touch && (!this.touchInputReady() || version !== this.touchInputVersion))
                throw new Error('Terminal changed or disconnected · image was not inserted');
            this.clearCopy();
            this.term.paste(path + ' ');
            this.term.focus();
        }
        catch (e) {
            if (touch)
                throw e;
            this.ev.onRefused(e instanceof Error ? e.message : String(e));
        }
    }
    fileLinks(y) {
        const buffer = this.term.buffer.active;
        const edge = (row) => {
            const b = buffer.getLine(row);
            if (!b?.getCell)
                return false;
            for (let x = b.length - 1; x >= 0; x--) {
                const cell = b.getCell(x);
                if (cell?.getChars().trim())
                    return x + cell.getWidth() >= this.term.cols - 2;
            }
            return false;
        };
        const hardContinuation = (row) => {
            const a = buffer.getLine(row)?.translateToString(true) ?? '';
            const b = buffer.getLine(row + 1)?.translateToString(true) ?? '';
            return !!a.trim() && /^[ \t]{0,16}[^\s│└┌┐┘├┤─•●]/.test(b) && (/[-/?&=#%]$/.test(a) || edge(row));
        };
        let first = y - 1;
        while (first > 0 && y - first < 256 && (buffer.getLine(first)?.isWrapped || hardContinuation(first - 1)))
            first--;
        if (buffer.getLine(first)?.isWrapped || first > 0 && hardContinuation(first - 1))
            return []; // never turn a bounded suffix into a different path
        const rows = [];
        const fullWidthBreaks = new Set();
        let line = '';
        for (let row = first; row < first + 256; row++) {
            const current = buffer.getLine(row);
            if (!current)
                break;
            const wrapped = !!buffer.getLine(row + 1)?.isWrapped;
            const text = current.translateToString(!wrapped);
            rows.push({ row, start: line.length, text });
            line += text;
            if (!wrapped) {
                if (!hardContinuation(row))
                    break;
                if (edge(row))
                    fullWidthBreaks.add(line.length);
                line += '\n';
            }
        }
        if (rows.length && (buffer.getLine(rows.at(-1).row + 1)?.isWrapped || hardContinuation(rows.at(-1).row)))
            return [];
        // xterm ranges use terminal cells; JS offsets count UTF-16 (emoji, CJK and combining accents differ).
        const position = (offset, end = false) => {
            const r = rows.find(r => offset < r.start + r.text.length) ?? rows[rows.length - 1];
            if (!r)
                return { x: 1, y };
            const b = buffer.getLine(r.row);
            const local = offset - r.start;
            let chars = 0;
            if (b.getCell)
                for (let x = 0; x < b.length; x++) {
                    const c = b.getCell(x), len = c?.getChars().length || (c?.getWidth() ? 1 : 0);
                    if (chars + len > local)
                        return { x: x + 1 + (end ? Math.max(0, (c?.getWidth() ?? 1) - 1) : 0), y: r.row + 1 };
                    chars += len;
                }
            return { x: local + 1, y: r.row + 1 };
        };
        const out = [];
        for (const hit of terminalReferences(line, fullWidthBreaks)) {
            const ref = hit.ref;
            if (ref.kind !== 'file' && ref.kind !== 'url' && !this.ev.onReference)
                continue;
            const start = position(hit.start), end = position(hit.end - 1, true);
            if (start.y > y || end.y < y)
                continue;
            out.push({ range: { start, end }, text: hit.text, activate: (e) => this.activateRef(ref, e) });
        }
        const columns = this.columnLinks(y);
        if (!columns.length)
            return out;
        // A path the CLI wrapped inside its own column replaces the truncated row-only guesses it overlaps.
        const covers = (l, c) => l.range.start.y <= y && l.range.end.y >= y
            && (l.range.start.y < y ? 1 : l.range.start.x) <= c.range.end.x && (l.range.end.y > y ? Infinity : l.range.end.x) >= c.range.start.x;
        return [...out.filter(l => !columns.some(c => covers(l, c))), ...columns];
    }
    /** Claude Code lays an attachment out in columns ("[image]" | path | "(152.9 KB)"): a long path continues on
     * the next row below its own first cell while the neighbouring columns keep their text. Join only those cells;
     * require the right-hand column to stay aligned on every row so ordinary prose is never stitched together. */
    columnLinks(y) {
        const buffer = this.term.buffer.active;
        const cells = (row) => {
            const b = buffer.getLine(row);
            if (!b?.getCell)
                return null;
            const out = [];
            for (let x = 0; x < b.length; x++) {
                const c = b.getCell(x);
                out.push(c?.getWidth() === 0 ? '\0' : c?.getChars() || ' ');
            }
            return out;
        };
        const blank = (r, x) => !r || x < 0 || x >= r.length || r[x] === ' ';
        const solid = (ch) => !!ch && ch !== ' ' && ch !== '\0' && ch.charCodeAt(0) < 0x2500;
        /** non-blank run starting at x; end is inclusive */
        const run = (r, x) => { let e = x; while (solid(r[e + 1]))
            e++; return e; };
        const out = [];
        for (let top = Math.max(0, y - 1 - 8); top <= y - 1; top++) {
            const first = cells(top);
            if (!first)
                continue;
            for (let start = 0; start < first.length; start++) {
                if (!solid(first[start]) || !blank(first, start - 1))
                    continue;
                const e = run(first, start), right = e + 2;
                const col = start;
                const segments = [{ row: top, start: col, end: e }];
                start = e;
                if (!blank(first, e + 1) || blank(first, right))
                    continue;
                // the cell above must not be a part of the same column, otherwise the longer chain from above wins
                const above = cells(top - 1);
                if (above && solid(above[col]) && solid(above[e]) && blank(above, e + 1) && !blank(above, right))
                    continue;
                for (let row = top + 1; row < top + 8; row++) {
                    const r = cells(row);
                    if (!r || !solid(r[col]))
                        break;
                    const end = run(r, col);
                    if (end > e)
                        break;
                    for (let x = end + 1; x < right; x++)
                        if (!blank(r, x)) {
                            segments.length = 0;
                            break;
                        }
                    if (!segments.length || blank(r, right)) {
                        segments.length = 0;
                        break;
                    }
                    segments.push({ row, start: col, end });
                    if (end < e)
                        break;
                }
                if (segments.length < 2 || segments.at(-1).row < y - 1 || top > y - 1)
                    continue;
                const rowsText = segments.map(g => cells(g.row).slice(g.start, g.end + 1).join(''));
                const head = rowsText[0].length;
                for (const hit of terminalReferences(rowsText.join(''))) {
                    if (hit.start >= head || hit.end <= head)
                        continue; // must span the column wrap
                    const ref = hit.ref;
                    if (ref.kind !== 'file' && ref.kind !== 'url' && !this.ev.onReference)
                        continue;
                    let offset = 0;
                    for (const [i, g] of segments.entries()) {
                        const len = rowsText[i].length;
                        const from = Math.max(hit.start, offset), to = Math.min(hit.end, offset + len);
                        if (g.row === y - 1 && from < to)
                            out.push({
                                range: { start: { x: g.start + 1 + from - offset, y }, end: { x: g.start + to - offset, y } },
                                text: hit.text, activate: (ev) => this.activateRef(ref, ev),
                            });
                        offset += len;
                    }
                }
            }
        }
        return out;
    }
    activateRef(ref, e) {
        if (ref.kind === 'url') {
            openUrl(ref.url, this.ev.onRefused, this.ev.onUrl);
            return;
        }
        if (this.ev.onReference && (!e.shiftKey || ref.kind !== 'file')) {
            this.ev.onReference(ref, e);
            return;
        }
        if (ref.kind !== 'file')
            return;
        const abs = ref.path.startsWith('/') ? ref.path : `${this.cwd}/${ref.path.replace(/^\.\//, '')}`;
        const rel = abs.startsWith(this.cwd + '/') ? abs.slice(this.cwd.length + 1) : '';
        const mod = e.metaKey || e.ctrlKey;
        if (!mod && !e.shiftKey && this.ev.onPathMenu)
            this.ev.onPathMenu(e, rel, abs, ref.line);
        else if (!e.shiftKey && (!mod || TERM_FLAGS.clickFiles) && rel && this.ev.onOpenFile)
            this.ev.onOpenFile(rel, ref.line, TERM_FLAGS.clickFiles && mod);
        else if (TERM_FLAGS.clickFiles && !e.shiftKey && !rel)
            this.ev.onOutside?.(abs, ref.line);
        else
            window.location.href = `idea://open?file=${encodeURIComponent(abs)}&line=${ref.line || 1}`;
    }
    armCopy() {
        if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write)
            return;
        this.pendingCopy?.(null);
        let filled = null;
        let failed = false;
        const text = new Promise((resolve, reject) => {
            const settle = (t) => {
                window.clearTimeout(timer);
                if (this.pendingCopy === settle)
                    this.pendingCopy = null;
                filled = t;
                if (t === null)
                    reject(new Error('no OSC 52 after the mouse release'));
                else {
                    resolve(t);
                    // The browser can reject write() before the socket delivers the selection: Safari refuses
                    // the write after a cancelled one at once, yet takes writeText() in the focused page.
                    if (failed)
                        this.copyText(t);
                }
            };
            const timer = window.setTimeout(() => settle(null), COPY_WAIT_MS);
            this.pendingCopy = settle;
        });
        // An immediately refused write may never consume the item's promise.
        void text.catch(() => { });
        void navigator.clipboard.write([new ClipboardItem({ 'text/plain': text })])
            .catch(() => {
            failed = true;
            if (filled !== null)
                this.copyText(filled);
        });
    }
    copyOsc52(text) {
        this.copiedSelection = text;
        if (this.pendingCopy)
            this.pendingCopy(text);
        else
            this.copyText(text);
    }
    clearCopy() {
        this.copiedSelection = '';
        this.pendingCopy?.(null);
    }
    copyText(text) {
        void navigator.clipboard?.writeText(text).catch((e) => this.copyFailed(e, text));
    }
    /** Run synchronously in the retry click: async clipboard permissions can remain denied. */
    copyWithSelection(text) {
        if (!document.execCommand)
            return false;
        const active = document.activeElement;
        const field = document.createElement('textarea');
        field.value = text;
        field.readOnly = true;
        field.style.position = 'fixed';
        field.style.opacity = '0';
        try {
            document.body.append(field);
            field.select();
            return document.execCommand('copy');
        }
        catch {
            return false;
        }
        finally {
            field.remove();
            if (active instanceof HTMLElement)
                active.focus({ preventScroll: true });
        }
    }
    /** outside a user gesture Safari refuses the write: the toast's button is the gesture */
    copyFailed(e, text) {
        const notify = this.ev.onCopyFailed ?? this.ev.onRefused;
        notify(`Copying was blocked by the browser (${e instanceof Error ? e.name : String(e)}). Click Copy to retry.`, { label: 'Copy', run: () => { if (!this.copyWithSelection(text))
                this.copyText(text); } });
    }
    key(e) {
        const nativeSelection = this.term.hasSelection();
        const clip = clipAction(e, nativeSelection || !!this.copiedSelection, MAC);
        if (clip === 'copy') {
            // without the async clipboard API the browser's copy event does it (xterm.js serves it)
            if (navigator.clipboard) {
                e.preventDefault();
                this.pendingCopy?.(null);
                const selection = nativeSelection ? this.term.getSelection() : this.copiedSelection;
                if (selection)
                    this.copyText(selection);
            }
            return false;
        }
        if (clip === 'paste')
            return false; // not to the pty: the browser fires its paste event
        if (e.type !== 'keydown')
            return true;
        const line = lineKey(e, MAC);
        if (line !== null) {
            this.clearCopy();
            this.send({ t: 'in', d: line });
            e.preventDefault();
            return false;
        }
        if (e.key === 'Enter' && (e.shiftKey || e.altKey) && !e.metaKey && !e.ctrlKey) {
            this.clearCopy();
            this.send({ t: 'in', d: '\x1b\r' });
            e.preventDefault();
            return false;
        }
        if (e.key === 'Enter' && e.shiftKey && appMod(e, MAC))
            return false; // 9.45: ⇧⌘⏎ = the pane alone / back (main.ts globalKeys)
        if (hostKey(e, this.id.startsWith('host-')))
            return false; // W2: main.ts globalKeys (⌥F12, ⌥⇧T / ⌥⇧W in a host shell)
        if (scratchKey(e))
            return false; // ⌥⇧S: New scratch file (main.ts globalKeys)
        // app shortcuts live on ⌘ (Alt off a Mac, appMod); everything else belongs to the terminal
        const k = appKey(e);
        if (appMod(e, MAC) && (APP_KEYS.includes(k) || (k === 'd' && TERM_FLAGS.dbSide) || (k === ']' && TERM_FLAGS.nav)) && (MAC || !TERM_KEEPS.includes(k)))
            return false; // ⌘D: the Database pane (module db)
        // Mouse reporting also uses onData/onBinary: invalidating there would cancel armCopy
        // on the very mouse release that requests the TUI's OSC 52.
        if (!['Meta', 'Control', 'Alt', 'Shift', 'CapsLock'].includes(e.key))
            this.clearCopy();
        // a typed command (/copy) answers with OSC 52 after this key: open the write inside the gesture
        if (TERM_FLAGS.armEnter && e.key === 'Enter' && !e.isComposing)
            this.armCopy();
        return true;
    }
    /** the attached terminal's size, not the pane's; the bar and the card say so */
    holdTtySize() {
        const { cols, rows } = this.pty;
        if (cols >= 10 && rows >= 3 && (this.term.cols !== cols || this.term.rows !== rows))
            this.term.resize(cols, rows);
        // shrink the font until the whole terminal fits the pane (readable down to FONT_READABLE;
        // smaller only after "Just look", and what still does not fit scrolls). The renderer takes a
        // new font size with its next frame: measure the drawn screen, adjust, measure again.
        const floor = this.cardDismissed ? FONT_MIN : FONT_READABLE;
        const key = `${cols}x${rows}|${this.host.clientWidth}x${this.host.clientHeight}|${floor}|${FONT}`;
        if (key !== this.fontKey) {
            this.fontKey = key;
            this.fontTries = 0;
        }
        const screen = this.host.querySelector('.xterm-screen');
        const font = Number(this.term.options.fontSize) || FONT;
        const w = screen?.offsetWidth ?? 0;
        const hgt = screen?.offsetHeight ?? 0;
        const fits = w > 0 && w <= this.host.clientWidth && hgt <= this.host.clientHeight;
        const ratio = w > 0 ? Math.min(this.host.clientWidth / w, this.host.clientHeight / Math.max(1, hgt)) : 1;
        let want = font;
        if (!fits)
            want = Math.min(FONT, Math.max(floor, Math.floor(font * ratio * 2) / 2 - (this.fontTries > 1 ? 0.5 : 0)));
        else if (font < FONT && ratio > 1.08)
            want = Math.min(FONT, Math.floor(font * ratio * 2) / 2); // the pane grew
        if (want !== font && this.fontTries < 6) {
            this.fontTries++;
            this.term.options.fontSize = want;
            window.clearTimeout(this.fontTimer);
            this.fontTimer = window.setTimeout(() => this.holdTtySize(), 80); // after the renderer's frame
        }
        const small = !fits && font <= floor;
        this.ttyBar.textContent = '';
        const b = document.createElement('b');
        b.textContent = markBidi(`⌨ attached in a terminal · ${cols}×${rows}`);
        const use = document.createElement('button');
        use.className = 'link small';
        use.textContent = 'Use here';
        use.title = 'take the session over: the terminal detaches (attach again there with piper attach) and this view sizes it';
        use.onclick = () => this.takeOver();
        const how = small ? ', this view scrolls' : want < FONT ? ', shown smaller here' : '';
        this.ttyBar.append(b, markBidi(` - that terminal sets the size${how}; typing here works too · `), use);
        this.ttyBar.title = 'piper attach: Ctrl-] in that terminal detaches it, then this view sizes the terminal again';
        this.ttyCard.classList.toggle('hidden', !small || this.cardDismissed);
        this.host.classList.toggle('overflow', small);
        if (small && !this.cardDismissed && !this.ttyCard.childElementCount) {
            const box = document.createElement('div');
            box.className = 'ttycard-box';
            const t = document.createElement('p');
            const strong = document.createElement('b');
            strong.textContent = 'Attached in a terminal';
            t.append(strong, document.createElement('br'), 'That terminal sets the size and it is larger than this pane. Read and answer in the Conversation, use the session here (the terminal detaches), or just look (it scrolls).');
            const conv = document.createElement('button');
            conv.className = 'primary';
            conv.textContent = 'Conversation';
            conv.onclick = () => this.ev.onConversation?.();
            const take = document.createElement('button');
            take.textContent = 'Use here';
            take.title = 'the terminal detaches (attach again there with piper attach) and this view sizes the session';
            take.onclick = () => this.takeOver();
            const show = document.createElement('button');
            show.textContent = 'Just look';
            show.onclick = () => {
                this.cardDismissed = true;
                this.ttyCard.classList.add('hidden');
                this.scheduleFit(); // smaller than readable now, the rest scrolls
                this.term.focus();
            };
            const row = document.createElement('div');
            row.className = 'row';
            row.append(conv, take, show);
            box.append(t, row);
            this.ttyCard.append(box);
        }
    }
    /** "Use here": ptyhost detaches the native terminal (tty-taken), then this view fits again */
    takeOver() {
        this.send({ t: 'take' });
        this.term.focus();
    }
    setTty(n) {
        const was = this.tty;
        this.tty = n > 0 ? 1 : 0;
        this.el.classList.toggle('ttyon', this.tty > 0);
        this.ttyBar.classList.toggle('hidden', this.tty === 0);
        if (this.tty === 0) {
            this.ttyCard.classList.add('hidden');
            this.cardDismissed = false;
            this.host.classList.remove('overflow');
            this.fontKey = '';
            this.term.options.fontSize = FONT; // before the fit below: the pane's own size at the normal font
        }
        if (was !== this.tty)
            this.lastSize = ''; // detached: this view sizes the PTY again (the next fit sends it)
        this.scheduleFit();
    }
    scheduleFit() {
        if (this.closed)
            return;
        window.clearTimeout(this.fitTimer);
        this.fitTimer = window.setTimeout(() => {
            if (document.hidden || this.frozen || this.replaying || !this.el.isConnected || this.el.offsetParent === null)
                return;
            if (this.tty) { // ptyhost would ignore our resize anyway
                this.holdTtySize();
                return;
            }
            try {
                this.fit.fit();
            }
            catch {
                return;
            }
            const size = `${this.term.cols}x${this.term.rows}`;
            if (size !== this.lastSize) {
                this.lastSize = size;
                const claim = this.claimNext;
                this.claimNext = false;
                this.send(claim ? { t: 'resize', c: this.term.cols, r: this.term.rows, claim } : { t: 'resize', c: this.term.cols, r: this.term.rows });
            }
        }, 50);
    }
    wantsStream() {
        return !terminalStreamVisibleOnly() || (this.inputState.visible && !document.hidden);
    }
    pauseStream() {
        this.streamCycle++;
        this.connectingFlush = false;
        window.clearTimeout(this.retryTimer);
        this.retryTimer = 0;
        const old = this.ws;
        this.ws = null;
        this.replaying = false;
        if (old) {
            old.close();
            this.ev.onConnection?.(false);
        }
        this.syncKeys();
    }
    syncStreaming() {
        if (!terminalStreamVisibleOnly() || !this.term.element || this.closed)
            return;
        if (!this.wantsStream())
            this.pauseStream();
        else if (!this.ws && !this.retryTimer)
            this.connect();
    }
    connect() {
        if (this.channelClosed)
            return;
        if (this.closed || !this.wantsStream() || this.connectingFlush || (this.ws && this.ws.readyState <= WebSocket.OPEN))
            return;
        window.clearTimeout(this.retryTimer);
        this.retryTimer = 0;
        // Draining old queued writes first makes the checkpoint describe the actual
        // emulator, even if the old stream closed before its parse callbacks ran.
        if (terminalStreamVisibleOnly()) {
            this.connectingFlush = true;
            const cycle = ++this.streamCycle;
            this.term.write('', () => {
                if (cycle !== this.streamCycle || this.closed)
                    return;
                this.connectingFlush = false;
                if (this.wantsStream())
                    this.connectNow();
            });
        }
        else
            this.connectNow();
    }
    connectNow() {
        // piperd may restart while the PTY keeps running. Preserve only an exact, verifiable native selection.
        const position = this.term.getSelectionPosition?.();
        if (this.processIdentity && position) {
            this.selectionReplay = { identity: this.processIdentity, text: this.term.getSelection(),
                x: position.start.x, y: position.start.y,
                length: (position.end.y - position.start.y) * this.term.cols + position.end.x - position.start.x,
                cols: this.term.cols, viewport: this.term.buffer?.active.viewportY };
        }
        this.clearCopy();
        const url = new URL(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${encodeURIComponent(reference(this.id).raw)}${terminalQuery(this.id)}`);
        if (terminalStreamVisibleOnly()) {
            url.searchParams.set('checkpoint', '1');
            if (this.checkpoint?.offset !== null && this.checkpoint) {
                url.searchParams.set('from', String(this.checkpoint.offset));
                url.searchParams.set('pid', String(this.checkpoint.pid));
                url.searchParams.set('started', String(this.checkpoint.started));
            }
        }
        const ws = terminalSocket(url.href);
        ws.binaryType = 'arraybuffer';
        this.ws = ws;
        this.replaying = true;
        this.touchKeys?.reset();
        this.syncKeys();
        // The replay was drawn at the PTY's size: buffer it until `hello` names that size, write it
        // at that size (a TUI replayed at 80x24 comes out garbled), then fit to the window - the
        // resize reaches Claude Code as SIGWINCH and it redraws at the real size.
        let hello = false;
        let orderedReplay = false;
        let checkpointProtocol = false;
        let wireEnd = null;
        let remaining = 0;
        let replayEnd = 0;
        const corrupt = () => {
            this.checkpoint = null;
            this.replaying = true;
            this.syncKeys();
            ws.close();
        };
        const write = (bytes) => {
            if (!checkpointProtocol) {
                this.term.write(bytes);
                return;
            }
            if (wireEnd === null || bytes.byteLength > remaining) {
                corrupt();
                return;
            }
            const end = wireEnd + bytes.byteLength, checkpoint = this.checkpoint, screen = this.screenGeneration;
            remaining -= bytes.byteLength;
            wireEnd = end;
            this.term.write(bytes, () => {
                if (!this.closed && this.screenGeneration === screen && this.checkpoint === checkpoint && checkpoint)
                    checkpoint.offset = end;
            });
        };
        let pending = [];
        let settleTimer = 0;
        let settleUntil = 0;
        const finishReplay = () => this.term.write('', () => {
            if (this.ws !== ws || this.closed)
                return;
            if (checkpointProtocol && remaining) {
                corrupt();
                return;
            }
            if (checkpointProtocol && this.checkpoint?.offset === null)
                this.checkpoint.offset = replayEnd;
            this.replaying = false;
            this.syncKeys();
            const saved = this.selectionReplay;
            this.selectionReplay = null;
            if (saved && saved.identity === this.processIdentity && saved.cols === this.term.cols && saved.length > 0 && !this.term.hasSelection()) {
                this.term.select(saved.x, saved.y, saved.length);
                if (this.term.getSelection() !== saved.text)
                    this.term.clearSelection();
                else if (saved.viewport !== undefined)
                    this.term.scrollToLine(saved.viewport);
            }
            if (terminalStreamVisibleOnly())
                this.ev.onConnection(true);
            this.scheduleFit();
        });
        const settle = () => {
            window.clearTimeout(settleTimer);
            settleTimer = window.setTimeout(() => {
                settleTimer = 0;
                finishReplay();
            }, Math.max(0, Math.min(150, settleUntil - Date.now())));
        };
        ws.onopen = () => {
            if (this.ws !== ws || this.closed || !this.wantsStream()) {
                ws.close();
                return;
            }
            ws.send(JSON.stringify({ auth: token(), replay_geometry: true }));
            this.retry = 0;
            this.syncKeys();
            if (!terminalStreamVisibleOnly())
                this.ev.onConnection(true);
        };
        ws.onmessage = (m) => {
            if (this.ws !== ws || this.closed)
                return;
            if (m.data instanceof ArrayBuffer) {
                if (!hello)
                    pending.push(new Uint8Array(m.data));
                else {
                    write(new Uint8Array(m.data));
                    if (settleTimer)
                        settle();
                }
                return;
            }
            let msg = {};
            try {
                msg = JSON.parse(String(m.data));
            }
            catch {
                return;
            }
            if (msg.t === 'hello') {
                this.channelClosed = false;
                this.channelReconnect = false;
                this.channelBanner.classList.add('hidden');
                this.processExited = false; // a reconnect may have missed the new process's respawn event
                this.syncKeys();
                hello = true;
                orderedReplay = msg.info?.replay_geometry === true;
                const info = msg.info;
                this.processIdentity = Number.isSafeInteger(info?.pid) && info.pid > 0 && Number.isFinite(info?.started) && info.started > 0 ? `${info.pid}:${info.started}` : '';
                if (this.selectionReplay?.identity !== this.processIdentity)
                    this.selectionReplay = null;
                checkpointProtocol = terminalStreamVisibleOnly() && msg.checkpoint === true && orderedReplay
                    && Number.isSafeInteger(info?.pid) && info.pid > 0 && Number.isFinite(info?.started) && info.started > 0
                    && Number.isSafeInteger(info?.bytes) && info.bytes >= 0;
                const resumed = checkpointProtocol && msg.resumed === true && this.checkpoint?.offset !== null && this.checkpoint
                    && this.checkpoint.pid === info.pid && this.checkpoint.started === info.started;
                if (msg.resumed && !resumed) {
                    corrupt();
                    return;
                }
                if (!resumed) {
                    this.screenGeneration++;
                    this.term.reset(); // a new process, history gap or old host requires a safe full replay
                    this.checkpoint = checkpointProtocol ? { pid: info.pid, started: info.started, offset: null } : null;
                }
                wireEnd = resumed ? this.checkpoint.offset : null;
                replayEnd = info?.bytes ?? 0;
                const cols = msg.info?.cols ?? 0;
                const rows = msg.info?.rows ?? 0;
                if (!resumed && cols >= 10 && rows >= 3)
                    this.term.resize(cols, rows);
                this.pty = { cols, rows };
                this.lastSize = `${cols}x${rows}`;
                this.tty = msg.info?.tty ? 1 : 0; // a reconnect: the terminal may have come or gone meanwhile
                this.el.classList.toggle('ttyon', this.tty > 0);
                this.ttyBar.classList.toggle('hidden', this.tty === 0);
                if (!this.tty) {
                    this.ttyCard.classList.add('hidden');
                    this.cardDismissed = false;
                    this.host.classList.remove('overflow');
                    this.term.options.fontSize = FONT;
                }
                const replay = pending;
                pending = [];
                for (const chunk of replay)
                    write(chunk);
                // fit once the replay has landed: 150 ms without output, at most 1 s after hello
                settleUntil = Date.now() + 1000;
                if (!orderedReplay)
                    settle(); // v8 PTY hosts do not send an explicit replay boundary
            }
            else if (msg.t === 'chunk' && hello && checkpointProtocol) {
                if (remaining || !Number.isSafeInteger(msg.off) || msg.off < 0 || !Number.isSafeInteger(msg.bytes)
                    || msg.bytes <= 0 || msg.bytes > 16 * 1024 * 1024 || !Number.isSafeInteger(msg.off + msg.bytes)
                    || (wireEnd !== null && wireEnd !== msg.off)) {
                    corrupt();
                    return;
                }
                wireEnd = msg.off;
                remaining = msg.bytes;
            }
            else if (msg.t === 'replay-end' && hello && orderedReplay) {
                finishReplay();
            }
            else if (msg.t === 'channel-closed') {
                this.closeChannel(msg.error || 'SSH terminal connection dropped');
            }
            else if (msg.t === 'exit') {
                this.processExited = true;
                this.syncKeys();
                this.ev.onExit(msg.rc ?? null);
            }
            else if (msg.t === 'respawn') {
                this.processIdentity = '';
                this.selectionReplay = null;
                this.processExited = false;
                this.touchKeys?.reset();
                this.syncKeys();
                this.clearCopy();
                this.screenGeneration++;
                this.checkpoint = null;
                checkpointProtocol = false;
                this.term.reset(); // Continue/Retry: the new sandbox's output starts on a clean screen
                this.lastSize = '';
                this.scheduleFit();
            }
            else if (msg.t === 'size') { // the PTY's new size (the attached terminal's)
                if (msg.replay) {
                    // Historical geometry belongs to the byte stream, not to current PTY
                    // ownership. Queue it after older writes; never fit or reclaim here.
                    const cols = msg.c ?? 0, rows = msg.r ?? 0;
                    const screen = this.screenGeneration;
                    this.term.write('', () => {
                        if (this.screenGeneration === screen && !this.closed && cols >= 10 && rows >= 3)
                            this.term.resize(cols, rows);
                    });
                    return;
                }
                this.pty = { cols: msg.c ?? this.pty.cols, rows: msg.r ?? this.pty.rows };
                if (this.tty)
                    this.scheduleFit();
                else {
                    // xterm writes asynchronously: finish older output before changing its geometry.
                    const { cols, rows } = this.pty;
                    const screen = this.screenGeneration;
                    this.term.write('', () => {
                        if (screen !== this.screenGeneration || this.closed)
                            return;
                        // 9.55: our own resize echoed, or another GUI's; the window the user is in answers a foreign one once
                        this.lastSize = `${cols}x${rows}`;
                        const now = Date.now();
                        const foreign = this.term.cols !== cols || this.term.rows !== rows;
                        // Output following this event uses the PTY geometry, even when this view yields.
                        // Keeping our previous grid corrupts cursor moves and wraps until the next reload.
                        if (foreign && cols >= 10 && rows >= 3)
                            this.term.resize(cols, rows);
                        // 9.155: a size claimed by ⋯ Redraw terminal elsewhere is followed, not answered
                        if (foreign && msg.claim)
                            this.reassertAt = now;
                        else if (foreign && document.hasFocus() && !document.hidden && now - this.reassertAt > REASSERT_MS) {
                            this.reassertAt = now;
                            this.scheduleFit();
                        }
                    });
                }
            }
            else if (msg.t === 'modes') {
                // 9.156: the terminal modes set before the replay start (mouse tracking, paste, alternate screen);
                // queued like the bytes, ahead of the first replayed one, and outside the checkpoint byte count
                if (typeof msg.d === 'string' && /^(\x1b\[\?[0-9]+[hl])+$/.test(msg.d))
                    this.term.write(msg.d);
            }
            else if (msg.t === 'tty') {
                this.setTty(msg.n ?? 0);
            }
            else if (msg.t === 'refused') {
                if (sshOnly(this.id) && msg.error === 'exited') {
                    this.closeChannel('SSH terminal connection dropped');
                    return;
                }
                const now = Date.now();
                if (now - this.refusedAt > 1500)
                    this.ev.onRefused(msg.error ?? 'refused');
                this.refusedAt = now;
            }
        };
        ws.onclose = (e) => {
            window.clearTimeout(settleTimer);
            if (this.ws !== ws)
                return;
            this.ws = null;
            this.replaying = false;
            this.touchKeys?.reset();
            this.syncKeys();
            this.ev.onConnection(false);
            if (!this.closed && reference(this.id).host) {
                this.closeChannel(e.code === 4401 ? 'Access refused. Reload Piper to authenticate' : this.channelReconnect ? 'Reconnect failed. Check the server connection and try again' : 'SSH terminal connection dropped');
                return;
            }
            if (this.closed || e.code === 4401 || !this.wantsStream())
                return;
            this.retry = Math.min(this.retry + 1, 6);
            this.retryTimer = window.setTimeout(() => { this.retryTimer = 0; this.connect(); }, 500 * 2 ** this.retry);
        };
    }
    closeChannel(reason) {
        this.channelClosed = true;
        this.channelReconnect = false;
        window.clearTimeout(this.retryTimer);
        this.retryTimer = 0;
        this.channelText.textContent = markBidi(`Terminal channel closed: ${reason}.`);
        this.channelDetail.textContent = markBidi(`${sshOnly(this.id) ? 'Remote session may still be running.' : 'Check the remote session status.'} Input is not resent.`);
        this.channelButton.textContent = 'Reconnect terminal';
        this.channelButton.disabled = false;
        this.channelBanner.classList.remove('hidden');
        this.touchKeys?.reset();
        this.syncKeys();
        this.ev.onConnection(false);
    }
    send(msg) {
        const input = msg.t === 'in';
        if (input && this.channelClosed) {
            this.drop('terminal channel closed');
            return;
        }
        if (input && terminalStreamVisibleOnly() && (this.replaying || !this.inputState.alive || this.processExited)) {
            this.drop('remote stream paused');
            return;
        }
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(msg));
            if (input)
                this.input.sentAt = Date.now();
        }
        else if (input)
            this.drop(this.ws ? `socket ${socketState(this.ws.readyState)}` : 'no socket');
    }
    /** 9.155: input xterm produced that never left (__piperTermDiag) */
    drop(why) {
        this.input.dropped++;
        this.input.dropAt = Date.now();
        this.input.dropWhy = why;
    }
    sendBinary(d) {
        if (this.channelClosed) {
            this.drop('terminal channel closed');
            return;
        }
        if (terminalStreamVisibleOnly() && (this.replaying || !this.inputState.alive || this.processExited)) {
            this.drop('remote stream paused');
            return;
        }
        if (this.ws?.readyState !== WebSocket.OPEN) {
            this.drop(this.ws ? `socket ${socketState(this.ws.readyState)}` : 'no socket');
            return;
        }
        const buf = new Uint8Array(d.length);
        for (let i = 0; i < d.length; i++)
            buf[i] = d.charCodeAt(i) & 0xff;
        this.ws.send(buf);
        this.input.sentAt = Date.now();
    }
}
function socketState(n) {
    return n === 0 ? 'connecting' : n === 1 ? 'open' : n === 2 ? 'closing' : 'closed';
}
//# sourceMappingURL=term.js.map