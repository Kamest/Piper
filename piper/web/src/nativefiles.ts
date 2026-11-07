/** Human-only native file actions. Paths are validated again by the host before reaching the OS. */
import { refHost } from './environment.js';
import type { MenuEntry } from './menu.js';
export const nativeFiles = (project = ''): boolean => !!window.__piperNative && window.__piperNative.platform !== 'win32' && !refHost(project);
export function nativeFileMenu(path: string, directory: boolean, toast: (s: string) => void, project = '', openable = true): MenuEntry[] {
  if (!nativeFiles(project)) return [];
  const mac = window.__piperNative!.platform === 'darwin';
  const run = (action: 'reveal' | 'open') => { void window.__piperNative!.file(action, path).catch(e => toast(e instanceof Error ? e.message : String(e))); };
  return [null, { group: mac ? 'ON THIS MAC' : 'ON THIS COMPUTER' },
    ...(directory && openable ? [{ label: mac ? 'Open folder in Finder' : 'Open folder in file manager', run: () => run('open') }] : []),
    { label: mac ? 'Reveal in Finder' : 'Show in file manager', keys: mac ? '⌥⌘R' : '', run: () => run('reveal') },
    ...(!directory && openable ? [{ label: 'Open with default app', run: () => run('open') }] : [])];
}
export function dragFile(el: HTMLElement, path: string, project = ''): void {
  if (refHost(project)) return;
  el.draggable = true;
  if (!nativeFiles(project)) el.title += (el.title ? ' · ' : '') + 'drag = path as text';
  el.addEventListener('dragstart', e => {
    if (nativeFiles(project)) { e.preventDefault(); window.__piperNative!.drag(path); }
    else { e.dataTransfer?.setData('text/plain', path); if (e.dataTransfer) e.dataTransfer.effectAllowed = 'copy'; }
  });
}
