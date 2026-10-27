// Copy/paste keys of the session terminal on every OS (term.ts key()).
//
// macOS: ⌘C copies either xterm's selection or the TUI selection received through OSC 52;
// ⌘V stays with the browser's paste event. Windows and Linux have no ⌘ - there Ctrl+C is the
// interrupt Claude Code needs
// and xterm.js turns Ctrl+V into a raw ^V, so neither ever copied or pasted. The usual terminal
// convention applies instead (Windows Terminal, GNOME Terminal, VS Code):
//
//   Ctrl+Shift+C, Ctrl+Insert, Ctrl+C WITH a selection   copy the selection
//   Ctrl+Shift+V, Ctrl+V, Shift+Insert                   paste (text, or an image upload)
//   Ctrl+C without a selection                            ^C, as before
//
// Paste is the browser's own paste event (term.ts lets the key through to it): text lands in
// xterm.js as a bracketed paste, an image in the capture handler that uploads it.

export type ClipAction = 'copy' | 'paste' | null;

export interface ClipKey {
  type: string;
  key: string;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  metaKey: boolean;
}

export function clipAction(e: ClipKey, hasSelection: boolean, mac: boolean): ClipAction {
  if (e.type !== 'keydown' || e.altKey) return null;
  const k = e.key.toLowerCase();
  if (mac) return e.metaKey && !e.ctrlKey && !e.shiftKey && k === 'c' && hasSelection ? 'copy' : null;
  if (e.metaKey) return null;
  if (e.ctrlKey && k === 'c' && (e.shiftKey || hasSelection)) return 'copy';
  if (e.ctrlKey && !e.shiftKey && k === 'insert') return 'copy';
  if (e.ctrlKey && k === 'v') return 'paste';
  if (e.shiftKey && !e.ctrlKey && k === 'insert') return 'paste';
  return null;
}

export function isMac(platform: string): boolean {
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** macOS input-line navigation: xterm.js does not translate ⌘← / ⌘→; shells and agent TUIs use ^A / ^E. */
export function lineKey(e: ClipKey & { isComposing?: boolean }, mac: boolean): string | null {
  if (!mac || e.type !== 'keydown' || !e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.isComposing) return null;
  return e.key === 'ArrowLeft' ? '\x01' : e.key === 'ArrowRight' ? '\x05' : null;
}

/** Piper's own shortcuts (⌘K, ⌘1…): ⌘ on a Mac, Alt elsewhere - on Windows the Win key belongs to
 *  the OS, Ctrl to the terminal and Ctrl+Alt is AltGr (a Czech keyboard types @ { } [ ] with it) */
export function appMod(e: Pick<ClipKey, 'ctrlKey' | 'altKey' | 'metaKey'>, mac: boolean): boolean {
  return mac ? e.metaKey : e.altKey && !e.ctrlKey && !e.metaKey;
}

/** a shortcut's label for this OS: the Mac glyphs stay on a Mac; elsewhere ⌘ is `mod` - Alt for Piper's
 *  own keys (appMod), Ctrl for the editor's (CodeMirror Mod-), the dialogs' and Database's (metaKey || ctrlKey);
 *  ⌥ is Alt, ⌃ Ctrl. Idempotent: an already converted label passes unchanged. */
export function keyLabel(s: string, mac: boolean, mod: 'Alt' | 'Ctrl' = 'Alt'): string {
  if (mac) return s;
  const out = s.replace(/⌘\/Ctrl\+?/g, 'Ctrl+').replace(/⌘\/(?=\S)/g, `${mod}/`).replace(/⌘\+?/g, `${mod}+`).replace(/⌥\+?/g, 'Alt+').replace(/⌃\+?/g, 'Ctrl+');
  const alts = out.split(' / ');   // "⌘+click / ⌥+click" is one key off a Mac
  return alts.length > 1 && alts.every((a) => a === alts[0]) ? alts[0]! : out;
}

const UI_MAC = typeof navigator !== 'undefined' && isMac(navigator.platform || navigator.userAgent);

/** keyLabel for the GUI's own OS */
export const kbd = (s: string, mod: 'Alt' | 'Ctrl' = 'Alt'): string => keyLabel(s, UI_MAC, mod);

const BY_POSITION: Record<string, string> = { BracketLeft: '[', BracketRight: ']', Backslash: '\\' };

/** the key of a shortcut; the number row and [ ] \ by their position (without ⇧): a Czech layout
 *  types + ě š č … and ú ) ¨ there, its [ ] \ need AltGr */
export function appKey(e: { key: string; code?: string; shiftKey?: boolean }): string {
  if (e.shiftKey) return e.key.toLowerCase();   // ⌘⇧2 is '@', not ⌘2 (as before)
  if (Object.values(BY_POSITION).includes(e.key)) return e.key;   // typed as such: a Czech Mac's ⌘⌥5 is ⌘[
  const m = /^Digit(\d)$/.exec(e.code ?? '');
  return m ? m[1]! : BY_POSITION[e.code ?? ''] ?? e.key.toLowerCase();
}

/** W2 host terminal: ⌥F12 (IntelliJ's Terminal) shows / hides the Terminal view from anywhere; inside a host shell
 *  ⌥⇧T opens another and ⌥⇧W closes it - the browser keeps ⌘T / ⌘W for itself. By position (e.code): ⌥ types
 *  other characters on a Mac. '' = not ours, the key goes to the terminal. */
export function hostKey(e: { code?: string; altKey: boolean; shiftKey: boolean; metaKey: boolean; ctrlKey: boolean }, inHost: boolean): '' | 'toggle' | 'new' | 'close' {
  if (!e.altKey || e.metaKey || e.ctrlKey) return '';
  if (e.code === 'F12' && !e.shiftKey) return 'toggle';
  if (!inHost || !e.shiftKey) return '';
  return e.code === 'KeyT' ? 'new' : e.code === 'KeyW' ? 'close' : '';
}

/** ⌥⇧S: New scratch file from anywhere (IDEA's ⌘⇧N is the browser's private window). By position: ⌥⇧S types Í
 *  on a Mac; Ctrl+Alt is AltGr off a Mac, never ours */
export function scratchKey(e: { code?: string; altKey: boolean; shiftKey: boolean; metaKey: boolean; ctrlKey: boolean }): boolean {
  return e.altKey && e.shiftKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyS';
}

/** off a Mac these Alt keys stay with the terminal: Alt+B / Alt+F move by word in bash and Claude
 *  Code, Alt+P is Claude Code's model picker (quick open is in the palette there) */
export const TERM_KEEPS = ['b', 'f', 'p'];

// OSC 52 (`ESC ] 52 ; <selection> ; <base64> BEL`): how a TUI puts text on the clipboard. Claude
// Code's full-screen UI selects with its own mouse handling and copies ONLY this way - xterm.js
// drops the sequence unless a handler is registered, so nothing it selected ever got copied.
// Write-only: a query (`?`) would hand the clipboard to the sandbox and is never answered.
export const OSC52_MAX = 1 << 20;

export function osc52Text(data: string): string | null {
  const semi = data.indexOf(';');
  if (semi < 0) return null;
  const b64 = data.slice(semi + 1);
  if (b64 === '?' || b64.length > OSC52_MAX * 4 / 3 + 4) return null;
  try {
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  } catch {
    return null;
  }
}
