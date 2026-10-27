// W11: the one popup menu of the GUI. The queue,
// the session bar, the editor, the stack, the inbox and the Database view all open it: groups, shortcuts on
// the right, a disabled item keeps its place and says why (tooltip), ↑↓ Home End skip it, Enter runs, Esc /
// Tab close and give the focus back. Eight items or more: typing filters them (speed search) - Backspace
// edits, the first Esc clears the filter, the second closes. Only one menu is open at a time.
import { clear, h } from './dom.js';
export const SEARCH_MIN = 8;
// ---- pure helpers (node --test)
export function labelText(l) {
    return typeof l === 'string' ? l : (l.textContent ?? '');
}
/** entries → rows: no separator at the start, the end or twice in a row; a group title without items under it goes */
export function normalizeEntries(entries) {
    const raw = [];
    for (const e of entries) {
        if (e === undefined || e === false)
            continue;
        if (e === null)
            raw.push({ kind: 'sep' });
        else if ('group' in e)
            raw.push({ kind: 'group', text: e.group });
        else
            raw.push({ kind: 'item', item: e });
    }
    const out = [];
    raw.forEach((r, i) => {
        if (r.kind === 'group') {
            const next = raw[i + 1];
            if (next?.kind !== 'item')
                return; // an empty group
        }
        if (r.kind === 'sep' && (!out.length || out[out.length - 1].kind === 'sep'))
            return;
        out.push(r);
    });
    while (out.length && out[out.length - 1].kind === 'sep')
        out.pop();
    return out;
}
/** the rows whose item text holds `q` (any case); groups without a match and every separator go while filtering */
export function filterEntries(rows, q) {
    const needle = q.trim().toLowerCase();
    if (!needle)
        return rows;
    const out = [];
    let group = null;
    for (const r of rows) {
        if (r.kind === 'group') {
            group = r;
            continue;
        }
        if (r.kind === 'sep') {
            group = null;
            continue;
        }
        if (!(r.item.search ?? labelText(r.item.label)).toLowerCase().includes(needle))
            continue;
        if (group) {
            out.push(group);
            group = null;
        }
        out.push(r);
    }
    return out;
}
export function itemCount(rows) {
    return rows.filter((r) => r.kind === 'item').length;
}
/** a menu key's next index among `n` enabled items (-1 = not a menu key): ↑ ↓ wrap, Home / End */
export function menuMove(n, at, key) {
    if (!n)
        return -1;
    if (key === 'ArrowDown')
        return at < 0 ? 0 : (at + 1) % n;
    if (key === 'ArrowUp')
        return at < 0 ? n - 1 : (at - 1 + n) % n;
    if (key === 'Home')
        return 0;
    if (key === 'End')
        return n - 1;
    return -1;
}
/** the menu's top-left: under an anchor right-aligned to it (above when it does not fit below), at a point
 *  from that point (shifted up / left to stay on screen); always 4 px inside the window */
export function placeMenu(at, w, hgt, vw, vh) {
    const clampX = (x) => Math.max(4, Math.min(x, vw - w - 4));
    const clampY = (y) => Math.max(4, Math.min(y, vh - hgt - 4));
    // 9.110: a DOMRect has x / y too - it was taken for a point and every anchored menu opened over its anchor
    if (!('bottom' in at))
        return { left: clampX(at.x), top: clampY(at.y) };
    const below = at.bottom + 4;
    const top = below + hgt > vh - 4 && at.top - hgt - 4 >= 4 ? at.top - hgt - 4 : below;
    return { left: clampX(at.right - w), top: clampY(top) };
}
/** opts.center: horizontally centred, a third down - where the eye is after a key from anywhere */
export function centerMenu(w, hgt, vw, vh) {
    return { left: Math.max(4, Math.round((vw - w) / 2)), top: Math.max(4, Math.round((vh - hgt) / 3)) };
}
/** Existing queue peek: beside its anchor, above/below if narrow, always within the visible area. */
export function peekPosition(at, w, hgt, vw, vh) {
    const clampY = (y) => Math.max(8, Math.min(y, vh - hgt - 8));
    let left = at.right + 10, top = clampY(at.top);
    if (left + w > vw - 8) {
        left = Math.max(8, vw - w - 8);
        top = clampY(at.top - hgt - 8 >= 8 ? at.top - hgt - 8 : at.bottom + 8);
    }
    return { left, top };
}
let current = null;
export function closeMenu() {
    current?.close(false);
}
/** the label with the first match of `q` marked; in a Node label, inside the text the item is searched by */
function marked(label, q, search) {
    const mark = (t) => {
        const i = t.toLowerCase().indexOf(q.toLowerCase());
        if (i < 0)
            return t;
        return h('span', {}, t.slice(0, i), h('mark', {}, t.slice(i, i + q.length)), t.slice(i + q.length));
    };
    if (typeof label === 'string')
        return q ? mark(label) : label;
    const copy = label.cloneNode(true);
    if (!q)
        return copy;
    const texts = [];
    const walk = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode())
        texts.push(n);
    const hit = texts.find((t) => search !== undefined && t.data === search) ?? texts.find((t) => t.data.toLowerCase().includes(q.toLowerCase()));
    if (hit)
        hit.replaceWith(mark(hit.data));
    return copy;
}
export function openMenu(at, entries, opts = {}) {
    current?.close(false);
    const anchor = at instanceof HTMLElement ? at : null;
    const was = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null;
    let rows = normalizeEntries(entries);
    const icons = rows.some((r) => r.kind === 'item' && r.item.icon !== undefined);
    const searchable = () => opts.search === true || (opts.search !== false && itemCount(rows) >= SEARCH_MIN);
    let q = '';
    let focusIdx = -1; // among the enabled items shown; -1 = the ticked one (IntelliJ: a popup opens on the current choice)
    const box = h('div', { class: 'cmenu' + (opts.className ? ` ${opts.className}` : ''), role: 'menu', 'aria-label': opts.label, tabindex: -1 });
    const list = h('div', { class: 'mlist' });
    const searchRow = h('div', { class: 'msearch', role: 'search' });
    box.append(searchRow, list);
    if (opts.foot)
        box.append(h('div', { class: 'mfoot' }, opts.foot));
    const enabled = () => [...list.querySelectorAll('button[role^=menuitem]:not(.msecondary):not([aria-disabled=true])')];
    function draw() {
        const focused = document.activeElement instanceof HTMLElement && list.contains(document.activeElement) ? document.activeElement : null;
        const focusId = focused?.dataset.menuId;
        const secondaryFocus = focused?.classList.contains('msecondary');
        const scrollTop = list.scrollTop;
        clear(list);
        const shown = filterEntries(rows, q);
        const hint = !q && !!opts.placeholder && searchable();
        searchRow.classList.toggle('hidden', !q && !hint);
        clear(searchRow);
        if (q)
            searchRow.append(h('span', { class: 'mq' }, q), h('span', { class: 'mcount' }, `${itemCount(shown)} of ${itemCount(rows)}`));
        else if (hint)
            searchRow.append(h('span', { class: 'mph' }, opts.placeholder));
        for (const r of shown) {
            if (r.kind === 'sep') {
                list.append(h('div', { class: 'sep', role: 'separator' }));
                continue;
            }
            if (r.kind === 'group') {
                list.append(h('div', { class: 'mtitle', role: 'presentation' }, r.text));
                continue;
            }
            const it = r.item;
            const off = !!it.disabled;
            const why = typeof it.disabled === 'string' ? it.disabled : it.title;
            const id = it.id ?? it.search ?? labelText(it.label);
            const b = h('button', {
                'data-menu-id': id,
                role: it.on === undefined ? 'menuitem' : 'menuitemcheckbox', class: [it.danger ? 'danger' : '', it.on ? 'on' : ''].filter(Boolean).join(' ') || undefined,
                'aria-disabled': off ? 'true' : undefined, 'aria-checked': it.on === undefined ? undefined : String(it.on), title: why, tabindex: -1,
                onclick: () => { if (off)
                    return; close(true); it.run(); },
            }, icons ? h('span', { class: 'micon', 'aria-hidden': 'true' }, it.icon?.startsWith('fi-') ? h('i', { class: `fi ${it.icon}` }) : it.icon ?? '') : null, h('span', { class: 'grow' }, marked(it.label, q, it.search)), it.hint ? h('span', { class: 'mhint', title: it.hint }, it.hint) : null, // 9.37: cut with an ellipsis, whole on hover
            it.keys ? h('kbd', {}, it.keys) : null, it.on ? h('i', { class: 'fi fi-check mcheck', 'aria-hidden': 'true' }) : null);
            if (it.secondary) {
                const action = it.secondary;
                const toggle = h('button', { class: 'msecondary', role: 'menuitemcheckbox', 'data-menu-id': id,
                    'aria-label': action.on ? action.activeLabel : action.label, title: action.on ? action.activeLabel : action.label,
                    'aria-checked': String(action.on), tabindex: -1, onclick: () => action.run(!action.on) }, action.icon.cloneNode(true));
                list.append(h('div', { class: 'mrow', role: 'presentation' }, b, toggle));
            }
            else
                list.append(b);
        }
        if (!shown.length)
            list.append(h('div', { class: 'mnone' }, 'No match'));
        const bs = enabled();
        const previous = focusId ? bs.findIndex((b) => b.dataset.menuId === focusId) : -1;
        if (previous >= 0)
            focusIdx = previous;
        if (focusIdx < 0)
            focusIdx = Math.max(0, bs.findIndex((b) => b.getAttribute('aria-checked') === 'true'));
        focusIdx = Math.min(focusIdx, Math.max(0, bs.length - 1));
        const target = bs[focusIdx];
        const secondary = secondaryFocus && target?.parentElement?.classList.contains('mrow') ? target.parentElement.querySelector('.msecondary') : null;
        if (secondary)
            secondary.focus({ preventScroll: !!focused });
        else
            (bs[focusIdx] ?? box).focus({ preventScroll: !!focused });
        if (focused)
            list.scrollTop = scrollTop;
    }
    function close(focus) {
        if (current !== handle)
            return;
        current = null;
        box.remove();
        anchor?.setAttribute('aria-expanded', 'false');
        document.removeEventListener('keydown', onKey, true);
        document.removeEventListener('mousedown', onDown, true);
        window.removeEventListener('blur', onBlur);
        window.removeEventListener('resize', onBlur);
        opts.closed?.();
        if (!focus)
            return;
        if (opts.back)
            opts.back();
        else
            (was?.isConnected ? was : anchor)?.focus({ preventScroll: true });
    }
    const handle = { close, update(entries) {
            if (current !== handle)
                return;
            rows = normalizeEntries(entries);
            draw();
            position();
        } };
    // capture: the menu's keys are the menu's only (the grid's Esc reverts edits, a dialog's Esc closes it)
    function onKey(e) {
        const stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
        const bs = enabled();
        const active = document.activeElement;
        const primary = active?.classList.contains('msecondary') ? active.parentElement?.querySelector('button:not(.msecondary)') : active;
        const next = menuMove(bs.length, bs.indexOf(primary), e.key);
        if (next >= 0) {
            stop();
            focusIdx = next;
            bs[next].focus();
            return;
        }
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
            const row = active?.closest('.mrow');
            const target = row?.querySelector(e.key === 'ArrowRight' ? '.msecondary' : 'button:not(.msecondary)');
            if (target) {
                stop();
                target.focus();
            }
            return;
        }
        if (e.key === 'Escape') {
            stop();
            if (q) {
                q = '';
                focusIdx = -1;
                draw();
            }
            else
                close(true);
            return;
        }
        if (e.key === 'Tab') {
            stop();
            close(true);
            return;
        }
        if (e.key === 'Enter' && e.shiftKey && opts.shiftEnter) {
            stop();
            close(true);
            opts.shiftEnter();
            return;
        }
        if (e.key === 'Enter' || (e.key === ' ' && (!q || active?.classList.contains('msecondary')))) {
            stop();
            const a = document.activeElement;
            if (a instanceof HTMLButtonElement && box.contains(a))
                a.click();
            return;
        }
        // a shortcut runs past the menu; one that took the anchor away (⌘G leaving the grid under "+ Add pane") takes the menu along
        if (anchor && (e.metaKey || e.ctrlKey || e.altKey))
            window.setTimeout(() => { if (!anchor.isConnected)
                close(false); }, 0);
        if (!searchable() || e.metaKey || e.ctrlKey || e.altKey)
            return;
        if (e.key === 'Backspace') {
            stop();
            if (q) {
                q = q.slice(0, -1);
                focusIdx = 0;
                draw();
            }
            return;
        }
        if (e.key.length === 1) {
            stop();
            q += e.key;
            focusIdx = 0;
            draw();
        }
    }
    function onDown(e) {
        if (e.target instanceof Node && (box.contains(e.target) || anchor?.contains(e.target)))
            return;
        close(false);
    }
    function onBlur() { close(false); }
    current = handle;
    document.body.append(box);
    anchor?.setAttribute('aria-haspopup', 'menu');
    anchor?.setAttribute('aria-expanded', 'true');
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onDown, true);
    window.addEventListener('blur', onBlur);
    window.addEventListener('resize', onBlur);
    draw();
    function position() {
        const r = box.getBoundingClientRect();
        const a = anchor?.getBoundingClientRect();
        const point = opts.side === 'right' && a ? { x: a.right + 8, y: a.bottom - r.height } : a ?? at;
        const pos = opts.center ? centerMenu(r.width, r.height, window.innerWidth, window.innerHeight)
            : placeMenu(point, r.width, r.height, window.innerWidth, window.innerHeight);
        box.style.left = `${pos.left}px`;
        box.style.top = `${pos.top}px`;
    }
    position();
    return handle;
}
/** an anchor that toggles its menu: a second click closes it */
export function toggleMenu(anchor, entries, opts = {}) {
    if (current && anchor.getAttribute('aria-expanded') === 'true') {
        closeMenu();
        return;
    }
    openMenu(anchor, entries(), opts);
}
//# sourceMappingURL=menu.js.map