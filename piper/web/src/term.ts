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
import { terminalQuery, reference } from './environment.js';
import { call, token } from './api.js';
import { terminalSocket, terminalStreamVisibleOnly, onTransportReady, type TerminalSocket } from './transport.js';
import { TERM_KEEPS, appKey, appMod, clipAction, hostKey, scratchKey, isMac, lineKey, osc52Text } from './clipkeys.js';
import { terminalTheme } from './theme.js';
import { piperUrl, references, type Reference } from './references.js';
import { markBidi } from './text.js';
import { TerminalKeys } from './termkeys.js';

/** opt-in behaviour (policy, via the GUI state - main.ts sets it on every poll) */
export const TERM_FLAGS = { hostLinks: false, armEnter: false, dbSide: false, clickFiles: false, nav: false };

/** Host mode opens the selected browser outside a Safari Dock app; failures stay visible. */
export function openUrl(uri: string, refused: (message: string) => void, internal?: (uri: string) => boolean): void {
  if (internal?.(uri)) return;
  if (piperUrl(uri, location.href || `${location.protocol}//${location.host}`, window.__PIPER__?.origins)) { refused('Open this link inside Piper'); return; }
  if (!/^https?:\/\//i.test(uri)) return;
  if (!TERM_FLAGS.hostLinks) { window.open(uri, '_blank', 'noopener'); return; }
  call('POST', '/api/open-url', { url: uri }).catch(() => refused('Could not open the host browser. Check Piper’s browser setting and that the browser is installed.'));
}

export interface TermEvents {
  /** Central navigation: internal Piper URLs never go to the host browser. */
  onUrl?(uri: string): boolean;
  onReference?(ref: Reference, event: MouseEvent): void;
  onCopyFailed?(message: string, retry: { label: string; run(): void }): void;
  onRefused(reason: string, retry?: { label: string; run(): void }): void;
  onExit(rc: number | null): void;
  onConnection(up: boolean): void;
  /** a pasted / dropped image: upload it, resolve to its path inside the sandbox */
  onImage(file: File): Promise<string>;
  /** ⌘-click on `path:line` (policy term_click_opens_files), or a plain one without onPathMenu: Piper's editor */
  /** full: the Files view instead of the pane beside the terminal (⌘/Ctrl+click, policy term_click_opens_files) */
  onOpenFile?(path: string, line: number, full?: boolean): void;
  /** a click on a file outside the session's checkout (policy term_click_opens_files): told, not opened */
  onOutside?(path: string, line: number): void;
  /** F04 (9.70): a plain click on `path:line` - a small menu (Files ↵, beside ⇧↵, IntelliJ, copy) at the click;
   *  rel = inside the checkout ('' = outside), abs = the IntelliJ path */
  onPathMenu?(e: MouseEvent, rel: string, abs: string, line: number): void;
  /** the terminal got the keyboard focus (grid: the clicked pane becomes the active session) */
  onFocus?(): void;
  /** "Conversation" on the attached-terminal card */
  onConversation?(): void;
}

// `src/main/java/Foo.java:123` (codenav FILE_LINK), relative to the session's project; the
// sandbox mounts the project at its host path, so the path is the IntelliJ path too

/** ⌘ shortcuts the GUI owns (main.ts globalKeys); every other key belongs to the terminal */
export const APP_KEYS = ['k', 'i', 'f', 'p', 'j', 'b', 'e', 'g', 'o', 'l', '[', '\\', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'];

const MAC = isMac(navigator.platform || navigator.userAgent);
const FONT = 13.5;
/** below this a terminal shrunk to fit its attached terminal's size is not readable: the card asks */
const FONT_READABLE = 9;
const FONT_MIN = 6;
/** how long a mouse release waits for Claude Code's OSC 52 before its clipboard write is dropped */
const COPY_WAIT_MS = 1500;
/** a foreign resize (another window or device) is answered once, then this view yields until the next focus */
const REASSERT_MS = 10_000;

/** 9.55: the PTY has one size and the last GUI window to send one wins (another tab, a second window, a
 *  phone). Coming back to this window - focus, visible again, back from the bfcache - takes it back. */
const LIVE = new Set<TermView>();
let reclaimWired = false;
function wireReclaim(): void {
  if (reclaimWired) return;
  reclaimWired = true;
  const reclaim = () => { for (const v of LIVE) v.reclaim(); };
  window.addEventListener('focus', reclaim);
  window.addEventListener('pageshow', reclaim);
  document.addEventListener('visibilitychange', reclaim);
}

export class TermView {
  readonly el: HTMLDivElement;
  /** pane bar (title, state), shown in the grid only */
  readonly label: HTMLDivElement;
  private host: HTMLDivElement;
  private term: Terminal;
  private fit = new FitAddon();
  readonly search = new SearchAddon();
  private ws: TerminalSocket | null = null;
  private closed = false;
  private retry = 0;
  private retryTimer = 0;
  private readonly stopReady: () => void;
  private fitTimer = 0;
  private frozen = false;
  /** the PTY's size as far as this view knows (what it sent, or a foreign `size`): a fit sends only a difference */
  private lastSize = '';
  private reassertAt = 0;
  private refusedAt = 0;
  /** between connect and the settled replay: no fit (it would redraw the replay at another size) */
  private replaying = false;
  /** Offset is committed only after xterm parsed it; queued output survives a suspended stream. */
  private checkpoint: { pid: number; started: number; offset: number | null } | null = null;
  private screenGeneration = 0;
  private streamCycle = 0;
  private connectingFlush = false;
  /** fills the clipboard write opened by the last mouse release (armCopy) */
  private pendingCopy: ((text: string | null) => void) | null = null;
  /** TUI-owned selection is not in xterm's selection manager; keep it for an explicit ⌘C. */
  private copiedSelection = '';
  /** a native terminal is attached and sizes the PTY (ptyhost's count, 0/1) */
  private tty = 0;
  private pty = { cols: 0, rows: 0 };
  /** the card over a pane too small for the attached terminal's size, until "Show here" */
  private ttyCard: HTMLDivElement;
  private ttyBar: HTMLDivElement;
  private cardDismissed = false;
  private fontKey = '';
  private fontTries = 0;
  private fontTimer = 0;
  private touchKeys: TerminalKeys | null = null;
  private inputState = { alive: false, active: false, label: '', visible: false };
  private processExited = false;

  /** A retained terminal/replay is not evidence of a live session. Main/Host supply process lifetime explicitly. */
  setInputState(alive: boolean, active: boolean, label: string, visible = active): void {
    this.inputState = { alive, active, label, visible };
    if (!this.touchKeys && typeof window.matchMedia === 'function') {
      this.touchKeys = new TerminalKeys({
        send: data => {
          if (!this.inputState.alive || !this.inputState.active || this.processExited || this.closed || this.ws?.readyState !== WebSocket.OPEN) return;
          this.clearCopy();
          if (TERM_FLAGS.armEnter && data === '\r') this.armCopy();
          this.term.input(data, true);
        },
        application: () => !!this.term.modes.applicationCursorKeysMode,
        typing: () => !!this.term.textarea && document.activeElement === this.term.textarea,
        keyboard: show => { if (show) this.term.focus(); else this.term.textarea?.blur(); },
        resize: () => this.scheduleFit(),
      });
      this.el.append(this.touchKeys.el);
    }
    this.syncStreaming();
    this.syncKeys();
  }

  private syncKeys(): void {
    this.touchKeys?.update({ ...this.inputState, alive: this.inputState.alive && !this.closed && !this.processExited,
      connected: this.ws?.readyState === WebSocket.OPEN && (!terminalStreamVisibleOnly() || !this.replaying) });
    if (terminalStreamVisibleOnly()) this.term.options.disableStdin = this.replaying || this.ws?.readyState !== WebSocket.OPEN || !this.inputState.alive || this.processExited;
  }

  constructor(readonly id: string, private cwd: string, private ev: TermEvents) {
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
    wrap.append(this.host, this.ttyCard);
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
    this.term.options.linkHandler = { activate: (_e: MouseEvent, uri: string) => openUrl(uri, this.ev.onRefused, this.ev.onUrl) };
    this.term.loadAddon(this.fit);
    this.term.loadAddon(this.search);
    this.term.loadAddon(new WebLinksAddon((_e: MouseEvent, uri: string) => openUrl(uri, this.ev.onRefused, this.ev.onUrl)));
    const uni = new Unicode11Addon();
    this.term.loadAddon(uni);
    this.term.unicode.activeVersion = '11';
    this.term.registerLinkProvider({ provideLinks: (y, cb) => cb(this.fileLinks(y)) });
    this.term.attachCustomKeyEventHandler((e) => this.key(e));
    // Claude Code copies its own (mouse) selection with OSC 52; a replay after a reload must
    // not overwrite the clipboard with whatever was copied back then
    this.term.parser.registerOscHandler(52, (data) => {
      const text = this.replaying ? null : osc52Text(data);
      if (text) this.copyOsc52(text);
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
    window.addEventListener('mouseup', (e) => {
      if (pressed && TERM_FLAGS.armEnter && !this.host.contains(e.target as Node)) this.armCopy();
      pressed = false;
    }, true);
    this.wireTouchScroll();
    this.term.onData((d) => this.send({ t: 'in', d }));
    this.term.onBinary((d) => this.sendBinary(d));
    this.stopReady = onTransportReady(() => {
      if (this.closed || !this.wantsStream()) return;
      window.clearTimeout(this.retryTimer); this.retryTimer = 0; this.retry = 0;
      if (!this.ws || this.ws.readyState > WebSocket.OPEN) { this.pauseStream(); this.connect(); }
    });
    LIVE.add(this);
    wireReclaim();
  }

  /** The DOM renderer draws block elements from the font, not as custom glyphs: with a line
   * height above 1 every row of Claude Code's logo (and any box art) gets a gap */
  private domRenderer(): void {
    this.term.options.lineHeight = 1;
    this.scheduleFit();
  }

  /** once: attach to the (stable) body and open the socket */
  mount(parent: HTMLElement): void {
    if (!this.el.isConnected) parent.append(this.el);
    if (!this.term.element) {
      this.term.open(this.host);
      this.term.textarea?.addEventListener('focus', () => { this.ev.onFocus?.(); this.syncKeys(); });
      this.term.textarea?.addEventListener('blur', () => this.syncKeys());
      try {
        const gl = new WebglAddon();
        gl.onContextLoss(() => {   // a lost context leaves a dead canvas: back to the DOM renderer
          gl.dispose();
          this.domRenderer();
        });
        this.term.loadAddon(gl);
      } catch {
        this.domRenderer();   // no WebGL (hardware acceleration off)
      }
      new ResizeObserver(() => this.scheduleFit()).observe(this.host);
      // "Just look" at a terminal larger than the pane: the wheel pans the pane while it can
      // (xterm would take every wheel event for its scrollback or Claude Code's mouse mode)
      this.host.addEventListener('wheel', (e) => {
        if (!this.host.classList.contains('overflow')) return;
        const h = this.host;
        const dx = e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX;
        const dy = e.shiftKey && !e.deltaX ? 0 : e.deltaY;
        const canX = (dx < 0 && h.scrollLeft > 0) || (dx > 0 && h.scrollLeft + h.clientWidth < h.scrollWidth);
        const canY = (dy < 0 && h.scrollTop > 0) || (dy > 0 && h.scrollTop + h.clientHeight < h.scrollHeight);
        if (!canX && !canY) return;   // at the edge: xterm scrolls its own history
        h.scrollBy(canX ? dx : 0, canY ? dy : 0);
        e.preventDefault();
        e.stopPropagation();
      }, { capture: true, passive: false });
      this.el.addEventListener('dragover', (e) => {
        if (e.dataTransfer?.types.includes('Files')) e.preventDefault();
      });
      this.el.addEventListener('drop', (e) => {
        const f = e.dataTransfer?.files[0];
        if (f && f.type.startsWith('image/')) {
          e.preventDefault();
          void this.image(f);
        }
      });
      // capture: xterm's own paste handler would send the (empty) text first
      this.el.addEventListener('paste', (e) => {
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

  focus(): void {
    // Navigation is for reading on phones; tapping xterm itself still opens the keyboard.
    if (!window.matchMedia?.('(pointer: coarse)').matches) this.term.focus();
  }

  /** Route a finger drag through xterm's wheel path: scrollback in a shell, mouse
   * reports in a TUI (its alternate buffer has no browser scrollback to pan). */
  private wireTouchScroll(): void {
    let remainder = 0;
    let touch: { id: number; x: number; y: number; moved: boolean } | null = null;
    this.host.addEventListener('touchstart', (e) => {
      remainder = 0;
      touch = e.touches.length === 1 ? { id: e.touches[0]!.identifier,
        x: e.touches[0]!.clientX, y: e.touches[0]!.clientY, moved: false } : null;
      // xterm's document gesture handler otherwise consumes the same drag a second time.
      e.stopPropagation();
    }, { passive: true });
    this.host.addEventListener('touchmove', (e) => {
      e.stopPropagation();
      if (!touch || e.touches.length !== 1) { touch = null; return; }
      const point = e.touches[0]!;
      if (point.identifier !== touch.id) return;
      const dy = touch.y - point.clientY;
      const dx = touch.x - point.clientX;
      if (!touch.moved && Math.hypot(dx, dy) < 8) return;
      touch.moved = true;
      touch.x = point.clientX;
      touch.y = point.clientY;
      e.preventDefault();
      // Dispatch on the screen, where xterm installs mouse reporting, then bubble
      // through the host's existing attached-terminal overflow handler.
      const screen = this.host.querySelector<HTMLElement>('.xterm-screen');
      if (!this.host.classList.contains('overflow')) {
        const rowHeight = (screen?.clientHeight || this.host.clientHeight) / this.term.rows;
        if (rowHeight > 0) {
          // A phone has little travel room. Preserve fractional rows across move events
          // and emit every TUI wheel step (xterm reports only one per wheel event).
          remainder += dy * 3;
          const lines = Math.trunc(remainder / rowHeight);
          remainder -= lines * rowHeight;
          if (this.term.modes.mouseTrackingMode === 'none' && this.term.buffer.active.type === 'normal') {
            if (lines) this.term.scrollLines(lines);
          } else {
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
    const end = (e: TouchEvent): void => {
      e.stopPropagation();
      if (touch?.moved) e.preventDefault(); // no synthetic click / keyboard after scrolling
      touch = null;
    };
    this.host.addEventListener('touchend', end, { passive: false });
    this.host.addEventListener('touchcancel', end, { passive: false });
  }

  /** type text into the prompt as a paste (no Enter): file references from the browser */
  insert(text: string): void {
    this.clearCopy();
    this.term.paste(text);
    this.term.focus();
  }

  /** after becoming visible again: re-measure (a hidden element has no size) */
  refit(): void {
    this.scheduleFit();
  }

  /** 9.55: this window is the user's again: fit, which sends the size only if the PTY has another one */
  reclaim(): void {
    if (this.closed) return;
    this.syncStreaming();
    if (document.hidden) return;
    this.reassertAt = 0;
    this.scheduleFit();
  }

  /** during rail/inspector animations: no SIGWINCH storm (Claude Code redraws on each) */
  freeze(on: boolean): void {
    this.frozen = on;
    if (!on) this.scheduleFit();
  }

  retheme(): void {
    this.term.options.theme = terminalTheme();
  }

  /** N1: a theme change recolours every open terminal (main, grid, host shells) */
  static rethemeAll(): void {
    for (const t of LIVE) t.retheme();
  }

  dispose(): void {
    this.closed = true;
    this.stopReady();
    this.pauseStream();
    this.touchKeys?.dispose();
    LIVE.delete(this);
    this.clearCopy();
    this.ws?.close();
    this.term.dispose();
    this.el.remove();
  }

  private async image(f: File): Promise<void> {
    try {
      const path = await this.ev.onImage(f);
      this.clearCopy();
      this.term.paste(path + ' ');
      this.term.focus();
    } catch (e) {
      this.ev.onRefused(e instanceof Error ? e.message : String(e));
    }
  }

  private fileLinks(y: number) {
    const buffer = this.term.buffer.active;
    let first = y - 1;
    while (first > 0 && y - first < 16 && buffer.getLine(first)?.isWrapped) first--;
    const rows: { row: number; start: number; text: string }[] = [];
    let line = '';
    for (let row = first; row < first + 32; row++) {
      const current = buffer.getLine(row);
      if (!current) break;
      const wrapped = !!buffer.getLine(row + 1)?.isWrapped;
      const text = current.translateToString(!wrapped);
      rows.push({ row, start: line.length, text }); line += text;
      if (!wrapped) break;
    }
    // xterm ranges use terminal cells; JS offsets count UTF-16 (emoji, CJK and combining accents differ).
    const position = (offset: number, end = false): { x: number; y: number } => {
      const r = rows.find(r => offset < r.start + r.text.length) ?? rows[rows.length - 1];
      if (!r) return { x: 1, y };
      const b = buffer.getLine(r.row)!;
      const local = offset - r.start;
      let chars = 0;
      if (b.getCell) for (let x = 0; x < b.length; x++) {
        const c = b.getCell(x), len = c?.getChars().length || (c?.getWidth() ? 1 : 0);
        if (chars + len > local) return { x: x + 1 + (end ? Math.max(0, (c?.getWidth() ?? 1) - 1) : 0), y: r.row + 1 };
        chars += len;
      }
      return { x: local + 1, y: r.row + 1 };
    };
    const out: { range: { start: { x: number; y: number }; end: { x: number; y: number } }; text: string;
      activate(e: MouseEvent, text: string): void }[] = [];
    for (const hit of references(line)) {
      const ref = hit.ref;
      if (ref.kind !== 'file' && !this.ev.onReference) continue;
      const start = position(hit.start), end = position(hit.end - 1, true);
      if (start.y > y || end.y < y) continue;
      out.push({
        range: { start, end },
        text: hit.text,
        activate: (e: MouseEvent) => {
          if (this.ev.onReference && (!e.shiftKey || ref.kind !== 'file')) { this.ev.onReference(ref, e); return; }
          if (ref.kind !== 'file') return;
          const abs = ref.path.startsWith('/') ? ref.path : `${this.cwd}/${ref.path.replace(/^\.\//, '')}`;
          const rel = abs.startsWith(this.cwd + '/') ? abs.slice(this.cwd.length + 1) : '';
          const mod = e.metaKey || e.ctrlKey;
          if (!mod && !e.shiftKey && this.ev.onPathMenu) this.ev.onPathMenu(e, rel, abs, ref.line);
          else if (!e.shiftKey && (!mod || TERM_FLAGS.clickFiles) && rel && this.ev.onOpenFile) this.ev.onOpenFile(rel, ref.line, TERM_FLAGS.clickFiles && mod);
          else if (TERM_FLAGS.clickFiles && !e.shiftKey && !rel) this.ev.onOutside?.(abs, ref.line);
          else window.location.href = `idea://open?file=${encodeURIComponent(abs)}&line=${ref.line || 1}`;
        },
      });
    }
    return out;
  }

  private armCopy(): void {
    if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return;
    this.pendingCopy?.(null);
    let filled: string | null = null;
    let failed = false;
    const text = new Promise<string>((resolve, reject) => {
      const settle = (t: string | null): void => {
        window.clearTimeout(timer);
        if (this.pendingCopy === settle) this.pendingCopy = null;
        filled = t;
        if (t === null) reject(new Error('no OSC 52 after the mouse release'));
        else {
          resolve(t);
          // The browser can reject write() before the socket delivers the selection: Safari refuses
          // the write after a cancelled one at once, yet takes writeText() in the focused page.
          if (failed) this.copyText(t);
        }
      };
      const timer = window.setTimeout(() => settle(null), COPY_WAIT_MS);
      this.pendingCopy = settle;
    });
    // An immediately refused write may never consume the item's promise.
    void text.catch(() => {});
    void navigator.clipboard.write([new ClipboardItem({ 'text/plain': text })])
      .catch(() => {
        failed = true;
        if (filled !== null) this.copyText(filled);
      });
  }

  private copyOsc52(text: string): void {
    this.copiedSelection = text;
    if (this.pendingCopy) this.pendingCopy(text);
    else this.copyText(text);
  }

  private clearCopy(): void {
    this.copiedSelection = '';
    this.pendingCopy?.(null);
  }

  private copyText(text: string): void {
    void navigator.clipboard?.writeText(text).catch((e: unknown) => this.copyFailed(e, text));
  }

  /** Run synchronously in the retry click: async clipboard permissions can remain denied. */
  private copyWithSelection(text: string): boolean {
    if (!document.execCommand) return false;
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
    } catch {
      return false;
    } finally {
      field.remove();
      if (active instanceof HTMLElement) active.focus({ preventScroll: true });
    }
  }

  /** outside a user gesture Safari refuses the write: the toast's button is the gesture */
  private copyFailed(e: unknown, text: string): void {
    const notify = this.ev.onCopyFailed ?? this.ev.onRefused;
    notify(`Copying was blocked by the browser (${e instanceof Error ? e.name : String(e)}). Click Copy to retry.`,
      { label: 'Copy', run: () => { if (!this.copyWithSelection(text)) this.copyText(text); } });
  }

  private key(e: KeyboardEvent): boolean {
    const nativeSelection = this.term.hasSelection();
    const clip = clipAction(e, nativeSelection || !!this.copiedSelection, MAC);
    if (clip === 'copy') {
      // without the async clipboard API the browser's copy event does it (xterm.js serves it)
      if (navigator.clipboard) {
        e.preventDefault();
        this.pendingCopy?.(null);
        const selection = nativeSelection ? this.term.getSelection() : this.copiedSelection;
        if (selection) this.copyText(selection);
      }
      return false;
    }
    if (clip === 'paste') return false;   // not to the pty: the browser fires its paste event
    if (e.type !== 'keydown') return true;
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
    if (e.key === 'Enter' && e.shiftKey && appMod(e, MAC)) return false;   // 9.45: ⇧⌘⏎ = the pane alone / back (main.ts globalKeys)
    if (hostKey(e, this.id.startsWith('host-'))) return false;   // W2: main.ts globalKeys (⌥F12, ⌥⇧T / ⌥⇧W in a host shell)
    if (scratchKey(e)) return false;   // ⌥⇧S: New scratch file (main.ts globalKeys)
    // app shortcuts live on ⌘ (Alt off a Mac, appMod); everything else belongs to the terminal
    const k = appKey(e);
    if (appMod(e, MAC) && (APP_KEYS.includes(k) || (k === 'd' && TERM_FLAGS.dbSide) || (k === ']' && TERM_FLAGS.nav)) && (MAC || !TERM_KEEPS.includes(k))) return false;   // ⌘D: the Database pane (module db)
    // Mouse reporting also uses onData/onBinary: invalidating there would cancel armCopy
    // on the very mouse release that requests the TUI's OSC 52.
    if (!['Meta', 'Control', 'Alt', 'Shift', 'CapsLock'].includes(e.key)) this.clearCopy();
    // a typed command (/copy) answers with OSC 52 after this key: open the write inside the gesture
    if (TERM_FLAGS.armEnter && e.key === 'Enter' && !e.isComposing) this.armCopy();
    return true;
  }

  /** the attached terminal's size, not the pane's; the bar and the card say so */
  private holdTtySize(): void {
    const { cols, rows } = this.pty;
    if (cols >= 10 && rows >= 3 && (this.term.cols !== cols || this.term.rows !== rows)) this.term.resize(cols, rows);
    // shrink the font until the whole terminal fits the pane (readable down to FONT_READABLE;
    // smaller only after "Just look", and what still does not fit scrolls). The renderer takes a
    // new font size with its next frame: measure the drawn screen, adjust, measure again.
    const floor = this.cardDismissed ? FONT_MIN : FONT_READABLE;
    const key = `${cols}x${rows}|${this.host.clientWidth}x${this.host.clientHeight}|${floor}`;
    if (key !== this.fontKey) {
      this.fontKey = key;
      this.fontTries = 0;
    }
    const screen = this.host.querySelector('.xterm-screen') as HTMLElement | null;
    const font = Number(this.term.options.fontSize) || FONT;
    const w = screen?.offsetWidth ?? 0;
    const hgt = screen?.offsetHeight ?? 0;
    const fits = w > 0 && w <= this.host.clientWidth && hgt <= this.host.clientHeight;
    const ratio = w > 0 ? Math.min(this.host.clientWidth / w, this.host.clientHeight / Math.max(1, hgt)) : 1;
    let want = font;
    if (!fits) want = Math.max(floor, Math.floor(font * ratio * 2) / 2 - (this.fontTries > 1 ? 0.5 : 0));
    else if (font < FONT && ratio > 1.08) want = Math.min(FONT, Math.floor(font * ratio * 2) / 2);   // the pane grew
    if (want !== font && this.fontTries < 6) {
      this.fontTries++;
      this.term.options.fontSize = want;
      window.clearTimeout(this.fontTimer);
      this.fontTimer = window.setTimeout(() => this.holdTtySize(), 80);   // after the renderer's frame
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
      t.append(strong, document.createElement('br'),
        'That terminal sets the size and it is larger than this pane. Read and answer in the Conversation, use the session here (the terminal detaches), or just look (it scrolls).');
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
        this.scheduleFit();   // smaller than readable now, the rest scrolls
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
  private takeOver(): void {
    this.send({ t: 'take' });
    this.term.focus();
  }

  private setTty(n: number): void {
    const was = this.tty;
    this.tty = n > 0 ? 1 : 0;
    this.el.classList.toggle('ttyon', this.tty > 0);
    this.ttyBar.classList.toggle('hidden', this.tty === 0);
    if (this.tty === 0) {
      this.ttyCard.classList.add('hidden');
      this.cardDismissed = false;
      this.host.classList.remove('overflow');
      this.fontKey = '';
      this.term.options.fontSize = FONT;   // before the fit below: the pane's own size at the normal font
    }
    if (was !== this.tty) this.lastSize = '';   // detached: this view sizes the PTY again (the next fit sends it)
    this.scheduleFit();
  }

  private scheduleFit(): void {
    window.clearTimeout(this.fitTimer);
    this.fitTimer = window.setTimeout(() => {
      if (document.hidden || this.frozen || this.replaying || !this.el.isConnected || this.el.offsetParent === null) return;
      if (this.tty) {   // ptyhost would ignore our resize anyway
        this.holdTtySize();
        return;
      }
      try {
        this.fit.fit();
      } catch {
        return;
      }
      const size = `${this.term.cols}x${this.term.rows}`;
      if (size !== this.lastSize) {
        this.lastSize = size;
        this.send({ t: 'resize', c: this.term.cols, r: this.term.rows });
      }
    }, 50);
  }

  private wantsStream(): boolean {
    return !terminalStreamVisibleOnly() || (this.inputState.visible && !document.hidden);
  }

  private pauseStream(): void {
    this.streamCycle++; this.connectingFlush = false;
    window.clearTimeout(this.retryTimer); this.retryTimer = 0;
    const old = this.ws; this.ws = null;
    this.replaying = false;
    if (old) { old.close(); this.ev.onConnection?.(false); }
    this.syncKeys();
  }

  private syncStreaming(): void {
    if (!terminalStreamVisibleOnly() || !this.term.element || this.closed) return;
    if (!this.wantsStream()) this.pauseStream();
    else if (!this.ws && !this.retryTimer) this.connect();
  }

  private connect(): void {
    if (this.closed || !this.wantsStream() || this.connectingFlush || (this.ws && this.ws.readyState <= WebSocket.OPEN)) return;
    window.clearTimeout(this.retryTimer); this.retryTimer = 0;
    // Draining old queued writes first makes the checkpoint describe the actual
    // emulator, even if the old stream closed before its parse callbacks ran.
    if (terminalStreamVisibleOnly()) {
      this.connectingFlush = true;
      const cycle = ++this.streamCycle;
      this.term.write('', () => {
        if (cycle !== this.streamCycle || this.closed) return;
        this.connectingFlush = false;
        if (this.wantsStream()) this.connectNow();
      });
    } else this.connectNow();
  }

  private connectNow(): void {
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
    let wireEnd: number | null = null;
    let remaining = 0;
    let replayEnd = 0;
    const corrupt = () => {
      this.checkpoint = null;
      this.replaying = true;
      this.syncKeys();
      ws.close();
    };
    const write = (bytes: Uint8Array) => {
      if (!checkpointProtocol) { this.term.write(bytes); return; }
      if (wireEnd === null || bytes.byteLength > remaining) { corrupt(); return; }
      const end = wireEnd + bytes.byteLength, checkpoint = this.checkpoint, screen = this.screenGeneration;
      remaining -= bytes.byteLength; wireEnd = end;
      this.term.write(bytes, () => {
        if (!this.closed && this.screenGeneration === screen && this.checkpoint === checkpoint && checkpoint) checkpoint.offset = end;
      });
    };
    let pending: Uint8Array[] = [];
    let settleTimer = 0;
    let settleUntil = 0;
    const finishReplay = () => this.term.write('', () => {
      if (this.ws !== ws || this.closed) return;
      if (checkpointProtocol && remaining) { corrupt(); return; }
      if (checkpointProtocol && this.checkpoint?.offset === null) this.checkpoint.offset = replayEnd;
      this.replaying = false;
      this.syncKeys();
      if (terminalStreamVisibleOnly()) this.ev.onConnection(true);
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
      if (this.ws !== ws || this.closed || !this.wantsStream()) { ws.close(); return; }
      ws.send(JSON.stringify({ auth: token(), replay_geometry: true }));
      this.retry = 0;
      this.syncKeys();
      if (!terminalStreamVisibleOnly()) this.ev.onConnection(true);
    };
    ws.onmessage = (m) => {
      if (this.ws !== ws || this.closed) return;
      if (m.data instanceof ArrayBuffer) {
        if (!hello) pending.push(new Uint8Array(m.data));
        else {
          write(new Uint8Array(m.data));
          if (settleTimer) settle();
        }
        return;
      }
      let msg: { t?: string; rc?: number | null; error?: string; info?: { cols?: number; rows?: number; tty?: number; replay_geometry?: boolean;
          pid?: number; started?: number; bytes?: number };
        c?: number; r?: number; n?: number; replay?: boolean; checkpoint?: boolean; resumed?: boolean; off?: number; bytes?: number } = {};
      try {
        msg = JSON.parse(String(m.data));
      } catch {
        return;
      }
      if (msg.t === 'hello') {
        this.processExited = false;   // a reconnect may have missed the new process's respawn event
        this.syncKeys();
        hello = true;
        orderedReplay = msg.info?.replay_geometry === true;
        const info = msg.info;
        checkpointProtocol = terminalStreamVisibleOnly() && msg.checkpoint === true && orderedReplay
          && Number.isSafeInteger(info?.pid) && info!.pid! > 0 && Number.isFinite(info?.started) && info!.started! > 0
          && Number.isSafeInteger(info?.bytes) && info!.bytes! >= 0;
        const resumed = checkpointProtocol && msg.resumed === true && this.checkpoint?.offset !== null && this.checkpoint
          && this.checkpoint.pid === info!.pid && this.checkpoint.started === info!.started;
        if (msg.resumed && !resumed) { corrupt(); return; }
        if (!resumed) {
          this.screenGeneration++;
          this.term.reset();   // a new process, history gap or old host requires a safe full replay
          this.checkpoint = checkpointProtocol ? { pid: info!.pid!, started: info!.started!, offset: null } : null;
        }
        wireEnd = resumed ? this.checkpoint!.offset : null;
        replayEnd = info?.bytes ?? 0;
        const cols = msg.info?.cols ?? 0;
        const rows = msg.info?.rows ?? 0;
        if (!resumed && cols >= 10 && rows >= 3) this.term.resize(cols, rows);
        this.pty = { cols, rows };
        this.lastSize = `${cols}x${rows}`;
        this.tty = msg.info?.tty ? 1 : 0;   // a reconnect: the terminal may have come or gone meanwhile
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
        for (const chunk of replay) write(chunk);
        // fit once the replay has landed: 150 ms without output, at most 1 s after hello
        settleUntil = Date.now() + 1000;
        if (!orderedReplay) settle();   // v8 PTY hosts do not send an explicit replay boundary
      } else if (msg.t === 'chunk' && hello && checkpointProtocol) {
        if (remaining || !Number.isSafeInteger(msg.off) || msg.off! < 0 || !Number.isSafeInteger(msg.bytes)
          || msg.bytes! <= 0 || msg.bytes! > 16 * 1024 * 1024 || !Number.isSafeInteger(msg.off! + msg.bytes!)
          || (wireEnd !== null && wireEnd !== msg.off)) { corrupt(); return; }
        wireEnd = msg.off!; remaining = msg.bytes!;
      } else if (msg.t === 'replay-end' && hello && orderedReplay) {
        finishReplay();
      } else if (msg.t === 'exit') {
        this.processExited = true;
        this.syncKeys();
        this.ev.onExit(msg.rc ?? null);
      } else if (msg.t === 'respawn') {
        this.processExited = false;
        this.touchKeys?.reset();
        this.syncKeys();
        this.clearCopy();
        this.screenGeneration++; this.checkpoint = null; checkpointProtocol = false;
        this.term.reset();   // Continue/Retry: the new sandbox's output starts on a clean screen
        this.lastSize = '';
        this.scheduleFit();
      } else if (msg.t === 'size') {   // the PTY's new size (the attached terminal's)
        if (msg.replay) {
          // Historical geometry belongs to the byte stream, not to current PTY
          // ownership. Queue it after older writes; never fit or reclaim here.
          const cols = msg.c ?? 0, rows = msg.r ?? 0;
          const screen = this.screenGeneration;
          this.term.write('', () => {
            if (this.screenGeneration === screen && !this.closed && cols >= 10 && rows >= 3) this.term.resize(cols, rows);
          });
          return;
        }
        this.pty = { cols: msg.c ?? this.pty.cols, rows: msg.r ?? this.pty.rows };
        if (this.tty) this.scheduleFit();
        else {
          // xterm writes asynchronously: finish older output before changing its geometry.
          const { cols, rows } = this.pty;
          const screen = this.screenGeneration;
          this.term.write('', () => {
            if (screen !== this.screenGeneration || this.closed) return;
            // 9.55: our own resize echoed, or another GUI's; the window the user is in answers a foreign one once
            this.lastSize = `${cols}x${rows}`;
            const now = Date.now();
            const foreign = this.term.cols !== cols || this.term.rows !== rows;
            // Output following this event uses the PTY geometry, even when this view yields.
            // Keeping our previous grid corrupts cursor moves and wraps until the next reload.
            if (foreign && cols >= 10 && rows >= 3) this.term.resize(cols, rows);
            if (foreign && document.hasFocus() && !document.hidden && now - this.reassertAt > REASSERT_MS) {
              this.reassertAt = now;
              this.scheduleFit();
            }
          });
        }
      } else if (msg.t === 'tty') {
        this.setTty(msg.n ?? 0);
      } else if (msg.t === 'refused') {
        const now = Date.now();
        if (now - this.refusedAt > 1500) this.ev.onRefused(msg.error ?? 'refused');
        this.refusedAt = now;
      }
    };
    ws.onclose = (e) => {
      window.clearTimeout(settleTimer);
      if (this.ws !== ws) return;
      this.ws = null;
      this.replaying = false;
      this.touchKeys?.reset();
      this.syncKeys();
      this.ev.onConnection(false);
      if (this.closed || e.code === 4401 || !this.wantsStream()) return;
      this.retry = Math.min(this.retry + 1, 6);
      this.retryTimer = window.setTimeout(() => { this.retryTimer = 0; this.connect(); }, 500 * 2 ** this.retry);
    };
  }

  private send(msg: { t: string; [key: string]: unknown }): void {
    if (msg.t === 'in' && terminalStreamVisibleOnly() && (this.replaying || !this.inputState.alive || this.processExited)) return;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private sendBinary(d: string): void {
    if (terminalStreamVisibleOnly() && (this.replaying || !this.inputState.alive || this.processExited)) return;
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    const buf = new Uint8Array(d.length);
    for (let i = 0; i < d.length; i++) buf[i] = d.charCodeAt(i) & 0xff;
    this.ws.send(buf);
  }
}
