// Shared image geometry; translation is measured from the center of the viewing surface.
import { fi, h } from './dom.js';
import { markBidi } from './text.js';

interface ImageViewDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
}

interface ImageSource {
  path: string;
  context?: string;
  /** A preview already owns this URL; the viewer must never revoke it. */
  url?: string;
  /** Explicit user navigation only. A newly acquired URL belongs to this viewer. */
  load?(): Promise<string>;
}

export interface ImageTransform { scale: number; x: number; y: number }

/** Fit the original without upscaling small images. Unmounted surfaces have no usable geometry yet. */
export function fitImage(width: number, height: number, roomWidth: number, roomHeight: number): number {
  return width > 0 && height > 0 && roomWidth > 0 && roomHeight > 0
    ? Math.min(1, roomWidth / width, roomHeight / height) : 1;
}

/** Keep at least the fit scale and prevent panning the image beyond its visible edges. */
export function clampImage(value: ImageTransform, width: number, height: number, roomWidth: number, roomHeight: number): ImageTransform {
  const scale = Math.max(fitImage(width, height, roomWidth, roomHeight), Math.min(8, value.scale));
  const limitX = Math.max(0, (width * scale - roomWidth) / 2);
  const limitY = Math.max(0, (height * scale - roomHeight) / 2);
  return { scale, x: Math.max(-limitX, Math.min(limitX, value.x)) || 0, y: Math.max(-limitY, Math.min(limitY, value.y)) || 0 };
}

/** Preserve the image point under a wheel/pinch anchor, including movement of a pinch's midpoint. */
export function zoomImageAt(value: ImageTransform, scale: number, x: number, y: number, dx = 0, dy = 0): ImageTransform {
  const ratio = scale / value.scale;
  return { scale, x: x + dx - (x - value.x) * ratio, y: y + dy - (y - value.y) * ratio };
}

/** Both Files previews retain their existing image and expose the same viewer entry point. */
export function imagePreview(image: HTMLImageElement, open: () => void, caption?: HTMLElement): HTMLElement {
  image.addEventListener('click', open);
  if (caption) caption.classList.add('image-caption');
  return h('div', { class: 'fimg' }, image, caption,
    h('button', { class: 'icon image-open', title: 'Open image viewer', 'aria-label': 'Open image viewer', onclick: open }, fi('expand')));
}

/** A user-opened image stays above its source view: no session selection, terminal remount or new agent protocol. */
export function openImageView(deps: ImageViewDeps, source: ImageSource): void {
  const name = source.path.split('/').pop() || 'Image';
  const image = h('img', { alt: name, draggable: 'false' });
  const info = h('span', { class: 'imageview-info', title: source.path }, source.context || source.path);
  const close = h('button', { class: 'icon imageview-close', 'aria-label': 'Close image', title: 'Close (Esc)', onclick: () => deps.closeModal() }, fi('x'));
  const save = h('button', { class: 'imageview-save', 'aria-label': 'Save original', title: 'Save original', disabled: true }, fi('down'), h('span', {}, 'Save original'));
  const status = h('div', { class: 'imageview-status', role: 'status' });
  const surface = h('div', { class: 'imageview-surface', tabindex: '0', 'aria-label': 'Image, use plus and minus to zoom and arrow keys to pan' }, image);
  const percent = h('button', { class: 'ghost imageview-percent', title: 'Toggle fit and actual size', 'aria-label': 'Toggle fit and actual size' });
  const minus = h('button', { class: 'ghost', title: 'Zoom out (−)', 'aria-label': 'Zoom out' }, fi('minus'));
  const plus = h('button', { class: 'ghost', title: 'Zoom in (+)', 'aria-label': 'Zoom in' }, fi('plus'));
  const fit = h('button', { class: 'ghost imageview-fit', title: 'Fit (0)', 'aria-pressed': 'true' }, 'Fit');
  const actual = h('button', { class: 'ghost imageview-actual', title: 'Actual size (1)', 'aria-pressed': 'false' }, '100%');
  const toolbar = h('div', { class: 'imageview-tools', role: 'group', 'aria-label': 'Image zoom', hidden: true }, minus, percent, plus,
    h('span', { class: 'imageview-divider', 'aria-hidden': 'true' }), fit, actual);
  const stage = h('div', { class: 'imageview-stage' }, surface, status, toolbar,
    h('span', { class: 'imageview-hint' }, 'Esc close · + − zoom · 0 fit · 1 100%'));
  const root = h('div', { class: 'imageview' },
    h('div', { class: 'imageview-head' }, close, h('div', { class: 'imageview-title' }, h('strong', { title: source.path }, name), info), save), stage);
  let value: ImageTransform = { scale: 1, x: 0, y: 0 };
  let fitted = true, loaded = false, disposed = false, version = 0, url = '', owned = false, downloaded = false;
  const room = () => {
    const style = getComputedStyle(surface), top = parseFloat(style.paddingTop), bottom = parseFloat(style.paddingBottom);
    const height = Math.max(0, surface.clientHeight - top - bottom);
    return { width: Math.max(0, surface.clientWidth - 24), height, y: top + height / 2 };
  };
  const size = () => { const r = room(); return [r.width, r.height] as const; };
  const fitScale = () => fitImage(image.naturalWidth, image.naturalHeight, ...size());
  const release = (target: string) => { if (downloaded) window.setTimeout(() => URL.revokeObjectURL(target), 60000); else URL.revokeObjectURL(target); };
  const draw = () => {
    if (!loaded) return;
    value = clampImage(value, image.naturalWidth, image.naturalHeight, ...size());
    image.style.width = `${image.naturalWidth}px`; image.style.height = `${image.naturalHeight}px`;
    image.style.top = `${room().y}px`;
    image.style.transform = `translate(-50%, -50%) translate(${value.x}px, ${value.y}px) scale(${value.scale})`;
    percent.textContent = markBidi(`${Math.round(value.scale * 100)}%`);
    fit.setAttribute('aria-pressed', String(fitted)); actual.setAttribute('aria-pressed', String(!fitted && Math.abs(value.scale - 1) < .001));
    minus.disabled = value.scale <= fitScale() + .00001; plus.disabled = value.scale >= 8;
    surface.classList.toggle('zoomed', value.scale > fitScale() + .00001);
  };
  const reset = () => { fitted = true; value = { scale: fitScale(), x: 0, y: 0 }; draw(); };
  const zoom = (scale: number, x = 0, y = 0, dx = 0, dy = 0, start = value) => {
    if (!loaded) return;
    fitted = false;
    value = zoomImageAt(start, Math.max(fitScale(), Math.min(8, scale)), x, y, dx, dy); draw();
  };
  const toggle = (x = 0, y = 0) => { if (value.scale > fitScale() + .00001) reset(); else zoom(Math.max(1, fitScale() * 2), x, y); };
  fit.onclick = reset; actual.onclick = () => zoom(1); percent.onclick = () => toggle();
  minus.onclick = () => zoom(value.scale / 1.5); plus.onclick = () => zoom(value.scale * 1.5);
  save.onclick = () => {
    if (!loaded || !url) return;
    downloaded = true;
    const link = h('a', { href: url, download: name });
    document.body.append(link); link.click(); link.remove();
  };
  const showError = (message: string) => {
    loaded = false; image.hidden = true; toolbar.hidden = true; save.disabled = true;
    status.replaceChildren(h('strong', {}, 'Image could not be opened'), h('p', {}, message),
      ...(source.load ? [h('button', { onclick: () => void acquire() }, 'Retry')] : []));
    status.hidden = false;
  };
  const acquire = async () => {
    const current = ++version;
    loaded = false; image.hidden = true; toolbar.hidden = true; save.disabled = true; status.hidden = false;
    status.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, 'Loading image…'));
    if (owned && url) release(url);
    url = ''; owned = false;
    try {
      const next = source.url || await source.load?.();
      if (!next) throw new Error('The image is no longer available.');
      if (disposed || current !== version) { if (!source.url) URL.revokeObjectURL(next); return; }
      url = next; owned = !source.url;
      image.onload = () => {
        if (disposed || current !== version) return;
        loaded = true; image.hidden = false; toolbar.hidden = false; status.hidden = true; save.disabled = false;
        info.textContent = markBidi(`${image.naturalWidth} × ${image.naturalHeight}${source.context ? ' · ' + source.context : ''}`);
        reset();
      };
      image.onerror = () => { if (!disposed && current === version) showError('The file could not be decoded as an image.'); };
      image.src = url;
    } catch (e) { if (!disposed && current === version) showError(e instanceof Error ? e.message : 'The image could not be loaded.'); }
  };

  type Point = { x: number; y: number };
  const pointers = new Map<number, Point>();
  let gesture = { value, x: 0, y: 0, distance: 0 }, moved = false, touchTime = 0, lastTouch = 0, lastPointerType = '', tap: Point = { x: 0, y: 0 };
  const point = (event: PointerEvent | MouseEvent): Point => { const r = surface.getBoundingClientRect(); return { x: event.clientX - r.left - r.width / 2, y: event.clientY - r.top - room().y }; };
  const midpoint = () => {
    const [a, b] = [...pointers.values()];
    return a && b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, distance: Math.hypot(a.x - b.x, a.y - b.y) }
      : { x: a?.x ?? 0, y: a?.y ?? 0, distance: 0 };
  };
  const rebase = () => { gesture = { value: { ...value }, ...midpoint() }; };
  surface.addEventListener('pointerdown', event => {
    if (!loaded || event.button > 0) return;
    event.preventDefault(); surface.focus({ preventScroll: true });
    lastPointerType = event.pointerType;
    pointers.set(event.pointerId, point(event));
    try { surface.setPointerCapture(event.pointerId); } catch { /* Synthetic browser probes do not own an active pointer. */ }
    if (pointers.size === 1) { moved = false; touchTime = performance.now(); }
    else { moved = true; lastTouch = 0; }
    rebase();
  });
  surface.addEventListener('pointermove', event => {
    if (!pointers.has(event.pointerId)) return;
    event.preventDefault(); pointers.set(event.pointerId, point(event));
    const center = midpoint(), dx = center.x - gesture.x, dy = center.y - gesture.y;
    if (Math.hypot(dx, dy) > 5 || pointers.size > 1) moved = true;
    if (pointers.size > 1 && gesture.distance > 0) zoom(gesture.value.scale * center.distance / gesture.distance, gesture.x, gesture.y, dx, dy, gesture.value);
    else { value = { ...gesture.value, x: gesture.value.x + dx, y: gesture.value.y + dy }; draw(); }
  });
  const end = (event: PointerEvent) => {
    if (!pointers.has(event.pointerId)) return;
    const at = point(event), now = performance.now();
    if (event.type === 'pointerup' && event.pointerType === 'touch' && pointers.size === 1 && !moved && now - touchTime < 300) {
      if (lastTouch && now - lastTouch < 350 && Math.hypot(at.x - tap.x, at.y - tap.y) < 30) { toggle(at.x, at.y); lastTouch = 0; }
      else { lastTouch = now; tap = at; }
    }
    pointers.delete(event.pointerId); rebase();
  };
  surface.addEventListener('pointerup', end); surface.addEventListener('pointercancel', end); surface.addEventListener('lostpointercapture', end);
  surface.addEventListener('dblclick', event => { if (lastPointerType === 'touch' && performance.now() - touchTime < 500) return; event.preventDefault(); const p = point(event); toggle(p.x, p.y); });
  surface.addEventListener('wheel', event => { event.preventDefault(); const p = point(event); zoom(value.scale * Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * .01), p.x, p.y); }, { passive: false });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); deps.closeModal(); return; }
    if (event.key === 'Tab') {
      const buttons = [...root.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]')].filter(el => el.checkVisibility());
      const next = buttons[(buttons.indexOf(document.activeElement as HTMLElement) + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length];
      if (next) { event.preventDefault(); event.stopPropagation(); next.focus({ preventScroll: true }); }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === '+' || event.key === '=') zoom(value.scale * 1.5);
    else if (event.key === '-') zoom(value.scale / 1.5);
    else if (event.key === '0') reset();
    else if (event.key === '1') zoom(1);
    else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
      value = { ...value, x: value.x + (event.key === 'ArrowLeft' ? 40 : event.key === 'ArrowRight' ? -40 : 0), y: value.y + (event.key === 'ArrowUp' ? 40 : event.key === 'ArrowDown' ? -40 : 0) }; draw();
    } else return;
    event.preventDefault(); event.stopPropagation();
  });

  deps.modal(name, root);
  close.focus({ preventScroll: true });
  // The window's own top chrome (the desktop title bar with the macOS window buttons, the update banner) stays
  // above every dialog: the full-window viewer starts below it, or its Close sits under the window buttons.
  const layer = root.closest<HTMLElement>('.olayer'), app = document.getElementById('app');
  const below = () => {
    if (!layer || !app) return;
    const covered = !!document.querySelector('#winbar.native, body > #update-banner');
    layer.style.top = covered ? `${Math.max(0, Math.round(app.getBoundingClientRect().top))}px` : '';
  };
  below();
  const observer = new ResizeObserver(() => { below(); if (fitted) reset(); else draw(); }); observer.observe(surface);
  if (app) observer.observe(app);
  // Back closes only the image; the underlying session's own navigation entry is unchanged.
  const historyKey = 'image-' + Math.random().toString(36).slice(2);
  let historyAdded = false;
  const back = (event: PopStateEvent) => {
    if ((event.state as { piperImage?: string } | null)?.piperImage === historyKey) return;
    event.stopImmediatePropagation();
    window.removeEventListener('popstate', back, true);
    if (!disposed) deps.closeModal();
  };
  try { history.pushState({ ...history.state, piperImage: historyKey }, ''); historyAdded = true; window.addEventListener('popstate', back, true); }
  catch { /* Sandboxed embedded viewers may not allow a history entry. Close and Escape still work. */ }
  const cleanup = () => {
    if (disposed) return;
    disposed = true; ++version; observer.disconnect(); pointers.clear(); image.onload = image.onerror = null;
    if (owned && url) release(url);
    if (historyAdded && (history.state as { piperImage?: string } | null)?.piperImage === historyKey) {
      history.back(); window.setTimeout(() => window.removeEventListener('popstate', back, true), 1000);
    } else window.removeEventListener('popstate', back, true);
  };
  root.closest('.olayer')?.addEventListener('close', cleanup, { once: true });
  void acquire();
}
