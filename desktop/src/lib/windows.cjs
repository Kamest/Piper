'use strict';

/**
 * 9.173 more Piper windows. Window 1 is the main window; the others load the same GUI with `?window=N` (2..99), which
 * gives each its own view keys in the GUI (environment.ts windowKey) - its session, layout and grid.
 */

/** The GUI address for window `slot`: the service's own URL (token in the fragment) plus `?window=N`. */
function windowURL(tokenURL, slot) {
  const u = new URL(tokenURL);
  if (slot > 1) u.searchParams.set('window', String(slot)); else u.searchParams.delete('window');
  return u.href;
}

/** The lowest free slot 2..99, 0 when every one is open. */
function freeSlot(used) {
  const taken = new Set(used);
  for (let i = 2; i < 100; i++) if (!taken.has(i)) return i;
  return 0;
}

const onScreen = (b, displays) => displays.some(({ workArea: a }) => b.x < a.x + a.width && b.x + b.width > a.x && b.y < a.y + a.height && b.y + b.height > a.y);

/**
 * BrowserWindow size/position: remembered bounds (their position only while some display still shows them - a
 * disconnected second monitor brings the window to the primary one), else beside `near` (the main window), else default.
 */
function placement(bounds, displays, near = null) {
  const b = bounds || {};
  if ([b.width, b.height].every(Number.isFinite) && b.width >= 620 && b.height >= 400) {
    return { width: b.width, height: b.height, ...([b.x, b.y].every(Number.isFinite) && onScreen(b, displays) ? { x: b.x, y: b.y } : {}) };
  }
  if (near && [near.x, near.y, near.width, near.height].every(Number.isFinite)) {
    const next = { width: near.width, height: near.height, x: near.x + 32, y: near.y + 32 };
    return onScreen(next, displays) ? next : { width: near.width, height: near.height };
  }
  return { width: 1040, height: 760 };
}

/** windows.json as written by saveWindows: at most 20 distinct slots 2..99 with their bounds; anything else dropped. */
function restorable(data) {
  if (!Array.isArray(data)) return [];
  const seen = new Set();
  return data.filter(w => w && Number.isInteger(w.slot) && w.slot >= 2 && w.slot <= 99 && !seen.has(w.slot) && seen.add(w.slot))
    .slice(0, 20).map(w => ({ slot: w.slot, bounds: w.bounds && typeof w.bounds === 'object' ? w.bounds : {} }));
}

module.exports = { windowURL, freeSlot, placement, restorable };
