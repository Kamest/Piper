/** Human-only native file actions. Paths are validated again by the host before reaching the OS. */
import { refHost } from './environment.js';
import type { MenuEntry } from './menu.js';
export const nativeFiles = (project = ''): boolean => !!window.__piperNative && (window.__piperNative.fileSupport ?? window.__piperNative.platform !== 'win32') && !refHost(project);
export function nativeFileMenu(path: string, _directory: boolean, toast: (s: string) => void, project = '', _openable = false): MenuEntry[] {
  if (!nativeFiles(project)) return [];
  const mac = window.__piperNative!.platform === 'darwin';
  // Preserve the call contract for older metadata, but never offer an unsupported native launch.
  const run = () => { void window.__piperNative!.file('reveal', path).catch(e => toast(e instanceof Error ? e.message : String(e))); };
  return [null, { group: mac ? 'ON THIS MAC' : 'ON THIS COMPUTER' },
    { label: mac ? 'Reveal in Finder' : 'Show in file manager', keys: mac ? '⌥⌘R' : '', run }];
}
export function dragFile(el: HTMLElement, path: string, project = '', prepareNow = false): void {
  if (refHost(project) || !path) return;
  el.draggable = true;
  let token = '', tokenAt = 0, pending = false;
  const prepare = () => {
    if (!nativeFiles(project) || !window.__piperNative!.prepareDrag || pending || (token && Date.now() - tokenAt < 25000)) return;
    token = ''; pending = true; delete el.dataset.nativeDrag;
    void window.__piperNative!.prepareDrag!(path).then(value => { token = value; tokenAt = Date.now(); el.dataset.nativeDrag = 'ready'; }).catch(() => {}).finally(() => { pending = false; });
  };
  // Preparation precedes the gesture: neither the API nor icon lookup may delay native startDrag.
  el.addEventListener('pointerenter', prepare);
  el.addEventListener('focus', prepare);
  el.addEventListener('pointerdown', prepare);
  // The file tree installs its drag handler during the first delegated pointerdown.
  if (prepareNow) prepare();
  if (!nativeFiles(project)) el.title += (el.title ? ' · ' : '') + 'drag = path as text';
  el.addEventListener('dragstart', e => {
    if (nativeFiles(project) && token) {
      e.preventDefault(); window.__piperNative!.drag(token); token = ''; delete el.dataset.nativeDrag;
    } else {
      e.dataTransfer?.setData('text/plain', path);
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'copy';
    }
  });
}
