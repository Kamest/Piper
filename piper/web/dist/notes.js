// Notes (module `notes`): one folder of the user's own files on this computer - not a project, not a session's.
// The view lists the folder on the left (the main note pinned, the rest newest first), edits a file in a hosted
// Files editor (`externalOnly`) and has a capture bar for a quick line. Agents reach the folder only through an
// approval card (phase 4); nothing here gives a session access.
import { ApiError, call } from './api.js';
import { appMod, kbd } from './clipkeys.js';
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';
import { createFiles } from './files.js';
import { nativeFiles } from './nativefiles.js';
/** who else sees the notes (decision 2026-11-12: agents by card only) - first run, phone and help say the same */
export const ACCESS_TEXT = 'A session sees them only after you allow it on a card';
/** System Settings › Privacy & Security › Files and Folders (desktop policy allows exactly this URL) */
export const PRIVACY_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders';
// ------------------------------------------------------------------ pure helpers (tests import these)
/** a JSON object or array (a pasted export, an API answer): saved as its own .json file, never as a line */
export function looksJson(text) {
    const s = text.trim();
    if (s[0] !== '{' && s[0] !== '[')
        return false;
    try {
        const v = JSON.parse(s);
        return typeof v === 'object' && v !== null;
    }
    catch {
        return false;
    }
}
/** what a pasted / typed text becomes: JSON or more than 8 lines = a file, otherwise a line in the main note */
export function autoMode(text) {
    if (looksJson(text))
        return { mode: 'file', why: 'json' };
    if (text.replace(/\n+$/, '').split('\n').length > 8)
        return { mode: 'file', why: 'long' };
    return { mode: 'line', why: '' };
}
const pad = (n) => String(n).padStart(2, '0');
export const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
/** the file name the backend will choose (notes.py create: `_stem`, a name's own extension, json / md, the default
 *  `note-YYYY-MM-DD-HHMM`); the backend adds -2, -3 when it is taken and says so */
export function noteFileName(name, text, now) {
    let stem = (name || '').replace(/[/\\\x00-\x1f\x7f]+/g, '-').trim().replace(/^\.+/, '').trim();
    stem = stem.replace(/\s+/g, ' ').slice(0, 100).replace(/[ .]+$/, '');
    let ext = '';
    const dot = stem.lastIndexOf('.');
    if (dot > 0 && /^[A-Za-z0-9]{1,10}$/.test(stem.slice(dot + 1))) {
        ext = stem.slice(dot + 1);
        stem = stem.slice(0, dot);
    }
    ext ||= looksJson(text) ? 'json' : 'md';
    stem ||= `note-${ymd(now)}-${pad(now.getHours())}${pad(now.getMinutes())}`;
    return `${stem}.${ext}`;
}
/** how a quick line lands (notes.py _entry): the day heading (stamp `time`, when it is not the last one) and the prefix */
export function entryPreview(stamp, now) {
    return {
        heading: stamp === 'time' ? `## ${ymd(now)}` : '',
        prefix: stamp === 'time' ? hm(now) : stamp === 'datetime' ? `${ymd(now)} ${hm(now)}` : '',
    };
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
/** the list's time column: now · 14:33 today · Yesterday · Tue (this week) · 3 Nov */
export function relTime(mtime, now) {
    const d = new Date(mtime * 1000);
    if (now.getTime() - d.getTime() < 60_000 && now.getTime() >= d.getTime())
        return 'now';
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((day(now) - day(d)) / 86_400_000);
    if (days === 0)
        return hm(d);
    if (days === 1)
        return 'Yesterday';
    if (days > 1 && days < 7)
        return DAYS[d.getDay()];
    return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : ''}`;
}
/** ⌘⇧J (Alt+Shift+J off the Mac): the quick note. `code` covers a layout whose J key types another letter */
export function isQuickNoteKey(e, mac) {
    if (!appMod(e, mac) || !e.shiftKey || (mac && (e.ctrlKey || e.altKey)))
        return false;
    const k = e.key.toLowerCase();
    return k === 'j' || (!/^[a-z]$/.test(k) && e.code === 'KeyJ');
}
/** the list: the main note first, then newest first; a filter keeps names containing it (case-insensitive) */
export function filterNotes(files, q) {
    const s = q.trim().toLowerCase();
    const rows = s ? files.filter((f) => f.name.toLowerCase().includes(s)) : files.slice();
    return rows.sort((a, b) => Number(b.main) - Number(a.main) || b.mtime - a.mtime || a.name.localeCompare(b.name));
}
/** the phone row's kind (board 07: `JSON · 2.1 KiB`) and its icon from the app's file icons (Files' scratch icons) */
const KINDS = { json: ['JSON', 'conf'], md: ['Markdown', 'doc'], markdown: ['Markdown', 'doc'], http: ['HTTP', 'http'],
    txt: ['Text', 'file'], log: ['Log', 'file'], csv: ['CSV', 'file'], sql: ['SQL', 'db'], yaml: ['YAML', 'conf'], yml: ['YAML', 'conf'], xml: ['XML', 'code'] };
export function noteKind(name) {
    const dot = name.lastIndexOf('.');
    const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
    const k = KINDS[ext];
    return k ? { label: k[0], icon: k[1] } : { label: ext && /^[a-z0-9]{1,10}$/.test(ext) ? ext.toUpperCase() : 'File', icon: 'file' };
}
/** where the files are, for the phone (board 07): the backend's default folder says whether it runs on a Mac */
export function whereText(s) {
    return s.default.startsWith('~/Documents/') ? 'on your Mac' : 'on your computer';
}
export function sizeText(n) {
    return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1).replace(/\.0$/, '')} KiB` : `${(n / 1048576).toFixed(1)} MiB`;
}
// ------------------------------------------------------------------ API
export const notesApi = {
    status: async () => (await call('GET', '/api/notes')).data,
    check: async (dir) => (await call('POST', '/api/notes/check', { dir })).data,
    search: async (q) => (await call('GET', '/api/notes/search?q=' + encodeURIComponent(q))).data,
    append: async (text) => (await call('POST', '/api/notes/append', { text })).data,
    file: async (text, name, line) => (await call('POST', '/api/notes/file', { text, name, line })).data,
    /** Settings › Notes › Notes folder (the human's choice; an agent never sets it) */
    setDir: async (dir) => { await call('POST', '/api/settings', { section: 'notes', values: { dir } }); },
};
/** the 409 body of a folder macOS refused ({state: 'denied', message, privacy, fallback}), else null */
export function deniedOf(e) {
    const b = e instanceof ApiError ? e.json : null;
    return b && typeof b === 'object' && b.state === 'denied' ? b : null;
}
/** "Copy path" / "Reveal in Finder" on a note or the folder */
export function copyPath(path, toast) {
    void navigator.clipboard?.writeText(path).then(() => toast('Path copied'), () => toast('Could not copy the path'));
}
export function revealPath(path, toast) {
    void window.__piperNative?.file('reveal', path).catch((e) => toast(e instanceof Error ? e.message : String(e)));
}
export const canReveal = () => nativeFiles();
export const revealLabel = () => window.__piperNative?.platform === 'darwin' ? 'Reveal in Finder' : 'Show in file manager';
/** board 09: macOS refused the folder - why, Open Privacy settings, Try again, Use ~/Piper Notes */
export function deniedBox(s, retry, fallback) {
    const where = s.shown || s.dir;
    const privacy = !!s.privacy;
    const mac = window.__piperNative?.platform === 'darwin';
    return h('div', { class: 'nt-denied', role: 'alert' }, h('div', { class: 'nt-denied-title' }, fi('alert'), privacy ? `macOS did not allow Piper to write to ${where}` : `Piper cannot use ${where}`), h('div', { class: 'nt-denied-text' }, privacy
        ? `Piper runs in the background, so macOS cannot ask you. Allow ${s.process || 'python3'} in System Settings › Privacy & Security › Files and Folders, then Try again. Or keep your notes in your home folder.`
        : s.message), h('div', { class: 'nt-denied-acts' }, privacy && mac ? h('button', { onclick: () => { window.open(PRIVACY_URL, '_blank', 'noopener'); } }, 'Open Privacy settings') : null, h('button', { onclick: retry }, fi('refresh'), 'Try again'), h('span', { class: 'grow' }), h('button', { class: 'primary', onclick: fallback }, `Use ${s.fallback || '~/Piper Notes'}`)));
}
export function createNotes(deps) {
    const el = h('div', { class: 'notes' });
    let status = null;
    let error = '';
    let q = '';
    let hits = [];
    let names = null;
    let searchTimer = 0;
    let loading = null;
    let shownKey = '';
    const capture = h('textarea', { class: 'nt-cap-in', rows: '1', placeholder: 'Add a line to notes.md…', 'aria-label': 'Add a line to the main note' });
    const capStamp = h('span', { class: 'nt-cap-stamp mut' });
    const captureBar = h('div', { class: 'nt-capture' }); // its children: placeCapture()
    const fit = () => { capture.style.height = 'auto'; capture.style.height = Math.min(capture.scrollHeight, 120) + 'px'; };
    capture.addEventListener('input', (e) => { e.stopPropagation(); fit(); });
    const submitCapture = () => {
        const text = capture.value.replace(/\s+$/, '');
        if (!text.trim() || capture.disabled)
            return;
        if (autoMode(text).why === 'json') {
            deps.quick('file', text);
            capture.value = '';
            fit();
            return;
        }
        capture.disabled = send.disabled = true;
        void appendLine(text).then((ok) => { if (ok) {
            capture.value = '';
            fit();
        } })
            .finally(() => { capture.disabled = send.disabled = false; if (!phone())
            capture.focus(); });
    };
    capture.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' || e.shiftKey || e.isComposing)
            return;
        e.preventDefault();
        submitCapture();
    });
    // phone (board 07): the same field over the whole width, with a 44 pt Append button instead of the key hints
    const phone = () => !!deps.phone?.();
    const send = h('button', { class: 'nt-ph-send primary', 'aria-label': 'Append the line', title: 'Append the line',
        onpointerdown: (e) => e.preventDefault(), onclick: submitCapture }, fi('up'));
    const capHint = h('span', { class: 'nt-cap-hint mut small' }, h('kbd', {}, '⏎'), ' append  ', h('kbd', {}, '⇧⏎'), ' new line');
    const phoneCap = h('div', { class: 'nt-ph-cap' });
    let phoneFile = ''; // phone: the note open over the list ('' = the list)
    const files = createFiles({
        toast: (t) => deps.toast(t), modal: deps.modal, closeModal: deps.closeModal,
        projects: () => [], insert: () => false, changed: () => new Set(), diff: () => { }, addNote: () => false, working: () => false,
        ...(deps.commands ? { commands: deps.commands } : {}),
        ...(deps.phone ? { phone: deps.phone } : {}),
        ...(deps.menuSheet ? { menuSheet: deps.menuSheet } : {}),
        externalOnly: {
            label: () => status?.shown || status?.dir || '',
            chip: 'AGENTS ONLY BY CARD',
            chipTitle: 'A session reads or writes these files only after you allow it on a card, for that session only.',
            footer: captureBar,
            mode: (id) => status && id.endsWith('//' + status.main) ? 'code' : undefined,
            empty: () => h('div', { class: 'nt-empty mut' }, 'Pick a note on the left, or write a line below.'),
        },
    });
    const filter = h('input', { class: 'nt-filter', type: 'search', placeholder: 'Filter by name or text', 'aria-label': 'Filter notes by name or text' });
    filter.addEventListener('input', () => {
        q = filter.value;
        window.clearTimeout(searchTimer);
        if (!q.trim()) {
            hits = [];
            names = null;
            drawList();
            return;
        }
        drawList();
        searchTimer = window.setTimeout(() => {
            const asked = q;
            notesApi.search(asked).then((r) => { if (asked === q) {
                hits = r.hits;
                names = r.names;
                drawList();
            } })
                .catch(() => { });
        }, 200);
    });
    filter.addEventListener('keydown', (e) => { if (e.key === 'Escape' && filter.value) {
        e.stopPropagation();
        filter.value = '';
        filter.dispatchEvent(new Event('input'));
    } });
    const rowsEl = h('div', { class: 'nt-rows', role: 'list' });
    const footEl = h('div', { class: 'nt-foot' });
    const listEl = h('aside', { class: 'nt-list' }, h('div', { class: 'nt-tools' }, h('button', { onclick: () => deps.quick('file') }, fi('new'), 'New file'), h('button', { onclick: () => void pasteAsFile() }, fi('copy'), 'Paste as file')), filter, rowsEl, footEl);
    const body = h('div', { class: 'nt-body' }, listEl, files.el);
    async function pasteAsFile() {
        let text = '';
        try {
            text = await navigator.clipboard.readText();
        }
        catch { /* not allowed: the overlay's field takes ⌘V */ }
        deps.quick('file', text);
    }
    function openNote(name, line = 0) {
        if (!status?.key)
            return;
        if (shownKey !== status.key) {
            files.show(status.key);
            shownKey = status.key;
        }
        if (phone() && phoneFile !== name) {
            phoneFile = name;
            draw();
        } // phone: the note is its own screen
        void files.openExternal(status.key, name, line).then(drawList);
    }
    /** phone: ‹ from a note back to the list (a pending edit saves first, like leaving Files) */
    function closePhoneFile() {
        files.flush();
        phoneFile = '';
        void load();
    }
    function row(f, now) {
        if (phone()) { // board 07: icon, the name over its kind and size (the main note: its newest entry), the time
            const k = noteKind(f.name);
            return h('button', { class: 'nt-ph-row' + (f.main ? ' main' : ''), role: 'listitem', onclick: () => openNote(f.name) }, fi(f.main ? 'notes' : k.icon, 'nt-ph-ic'), h('span', { class: 'nt-ph-text' }, h('span', { class: 'nt-name' }, f.name), h('span', { class: 'nt-ph-sub' }, f.main ? (f.last || 'Your main note') : `${k.label} · ${sizeText(f.size)}`)), h('span', { class: 'nt-time' }, relTime(f.mtime, now)));
        }
        const id = status?.key ? `${status.key}//${f.name}` : '';
        const on = id && files.activeId() === id;
        return h('button', { class: 'nt-row' + (on ? ' on' : '') + (f.main ? ' main' : ''), role: 'listitem', title: f.name, onclick: () => openNote(f.name) }, h('span', { class: 'nt-name' }, f.name), f.main ? fi('pin', 'nt-pin') : null, h('span', { class: 'nt-time mut small' }, relTime(f.mtime, now)));
    }
    function drawList() {
        clear(rowsEl);
        const s = status;
        if (!s || s.state !== 'ok')
            return;
        const now = new Date();
        const shown = filterNotes(s.files, '').filter((f) => !q.trim() || f.name.toLowerCase().includes(q.trim().toLowerCase()) || names?.includes(f.name) || hits.some((x) => x.name === f.name));
        const main = shown.filter((f) => f.main);
        const rest = shown.filter((f) => !f.main);
        rowsEl.append(...main.map((f) => row(f, now)));
        if (rest.length)
            rowsEl.append(...(phone() ? [] : [h('div', { class: 'nt-group' }, q.trim() ? 'MATCHES' : 'FILES · newest first')]), ...rest.map((f) => row(f, now)));
        if (q.trim() && hits.length) {
            rowsEl.append(h('div', { class: 'nt-group' }, 'IN THE TEXT'), ...hits.slice(0, 50).map((x) => h('button', { class: 'nt-hit', role: 'listitem', onclick: () => openNote(x.name, x.line) }, h('span', { class: 'nt-name' }, `${x.name}:${x.line}`), h('span', { class: 'nt-hit-text mut small' }, x.text))));
        }
        if (!shown.length && !(q.trim() && hits.length))
            rowsEl.append(h('div', { class: 'nt-none mut small' }, q.trim() ? 'Nothing matches.' : 'No notes yet.'));
        if (s.truncated)
            rowsEl.append(h('div', { class: 'nt-none mut small' }, 'Only the newest files are listed.'));
    }
    function drawFoot() {
        clear(footEl);
        const s = status;
        if (!s)
            return;
        const where = s.shown || s.dir;
        footEl.append(h('div', { class: 'nt-foot-label mut small' }, 'FOLDER'), h('div', { class: 'nt-foot-path', title: s.path || where }, h('span', {}, where), s.state === 'ok' ? h('span', { class: 'mut small' }, ` ${s.count} ${s.count === 1 ? 'file' : 'files'}`) : null), h('div', { class: 'nt-foot-acts' }, canReveal() && s.path && s.state === 'ok' ? h('button', { class: 'mini', onclick: () => revealPath(s.path, deps.toast) }, fi('diro'), revealLabel()) : null, h('button', { class: 'mini', onclick: () => copyPath(s.path || where, deps.toast) }, fi('copy'), 'Copy path')));
    }
    function drawStamp() {
        const p = entryPreview(status?.stamp ?? 'time', new Date()).prefix;
        capStamp.textContent = markBidi(p);
        capStamp.hidden = !p;
        capture.placeholder = markBidi(`Add a line to ${status?.main || 'notes.md'}…`);
    }
    /** board 06: the folder does not exist yet - what Notes is for, where the files go, the first note */
    function firstRun(s) {
        const tile = (k, title, text) => h('div', { class: 'nt-tile' }, h('div', { class: 'nt-tile-k' }, k), h('strong', {}, title), h('div', { class: 'mut small' }, text));
        const finder = whereText(s) === 'on your Mac' ? 'in Finder' : 'in your file manager';
        return h('div', { class: 'nt-first' }, h('div', { class: 'nt-first-card' }, h('h2', {}, 'One place for a sentence and for the file you need later'), h('div', { class: 'mut' }, `A line from a meeting, an idea during development, a JSON you want to mail tomorrow. Everything is a plain file in one folder you can find ${finder}.`), h('div', { class: 'nt-tiles' }, tile(fi('notes'), phone() ? 'Add a line' : `${kbd('⌘⇧J')} adds a line`, `From anywhere in Piper into ${s.main || 'notes.md'}, with the time, under today’s date.`), tile(fi('copy'), 'Paste becomes a file', 'JSON, a log or a request is saved under a name you choose.'), tile(fi('diro'), 'Easy to find again', canReveal() ? `${revealLabel()}, copy the path, or drag the file straight into a mail message.`
            : 'Filter by name or by text, copy the path or save a copy where you need it.')), h('div', { class: 'nt-first-folder' }, h('span', { class: 'mut small' }, 'Folder'), h('strong', {}, s.shown || s.dir), h('span', { class: 'mut small grow' }, 'Not created yet - Piper creates it with your first note.'), h('button', { class: 'mini', onclick: () => deps.settings() }, 'Change…')), h('div', { class: 'nt-first-acts' }, h('button', { class: 'primary', onclick: () => deps.quick('line') }, fi('plus'), 'Write the first note'), h('button', { onclick: () => void pasteAsFile() }, 'Paste as file'), h('span', { class: 'grow' }), h('span', { class: 'mut small' }, ACCESS_TEXT))));
    }
    /** phone (board 07): ‹ Notes and Paste as file on top; ‹ goes back to More, from a note back to the list */
    function phoneBar() {
        const inFile = !!phoneFile;
        return h('div', { class: 'nt-ph-bar' }, h('button', { class: 'icon nt-ph-back', 'aria-label': inFile ? 'Back to Notes' : 'Back', onclick: () => (inFile ? closePhoneFile() : deps.back?.()) }, fi('chev-l')), h('strong', {}, 'Notes'), h('span', { class: 'grow' }), inFile ? null : h('button', { class: 'icon nt-ph-paste', 'aria-label': 'Paste as file', title: 'Paste as file', onclick: () => void pasteAsFile() }, fi('copy')));
    }
    /** the capture field: on the phone list with its Append button, otherwise under the editor with the key hints */
    function placeCapture(onPhone) {
        if (onPhone) {
            captureBar.replaceChildren();
            phoneCap.replaceChildren(h('div', { class: 'nt-ph-field' }, capStamp, capture), send);
        }
        else {
            phoneCap.replaceChildren();
            captureBar.replaceChildren(capStamp, capture, capHint);
        }
    }
    function draw() {
        clear(el);
        const s = status;
        const onPhone = phone();
        el.classList.toggle('nt-phone', onPhone);
        files.setNarrow(onPhone); // the editor's phone form: a compact head, its actions in ⋯
        if (!onPhone)
            phoneFile = '';
        placeCapture(onPhone);
        if (onPhone)
            el.append(phoneBar());
        if (!s) {
            el.append(h('div', { class: 'nt-msg mut' }, error || 'Loading…'));
            return;
        }
        drawStamp();
        drawFoot();
        if (s.state !== 'ok')
            phoneFile = '';
        if (onPhone && s.state === 'ok') {
            if (phoneFile) {
                el.append(files.el);
                return;
            }
            const where = s.shown || s.dir;
            el.append(h('div', { class: 'nt-ph-where', title: s.path || where }, fi('diro'), h('span', { class: 'nt-ph-dir' }, where), h('span', { class: 'mut' }, whereText(s))), rowsEl, phoneCap);
            if (s.key && shownKey !== s.key) {
                files.show(s.key);
                shownKey = s.key;
            }
            drawList();
            return;
        }
        if (s.state === 'missing') {
            el.append(firstRun(s));
            return;
        }
        if (s.state === 'denied') {
            el.append(h('div', { class: 'nt-first' }, h('div', { class: 'nt-first-card' }, h('h2', {}, 'Notes'), deniedBox(s, () => void load(), () => void deps.setDir(s.fallback).then(load)))));
            return;
        }
        if (s.state === 'refused') {
            el.append(h('div', { class: 'nt-first' }, h('div', { class: 'nt-first-card' }, h('div', { class: 'nt-refused', role: 'alert' }, fi('alert'), h('span', {}, s.message)), h('div', { class: 'nt-first-acts' }, h('button', { class: 'primary', onclick: () => deps.settings() }, 'Choose another folder…')))));
            return;
        }
        body.replaceChildren(listEl, files.el);
        el.append(body);
        if (s.key && shownKey !== s.key) {
            files.show(s.key);
            shownKey = s.key;
            if (!files.activeId())
                void files.openExternal(s.key, s.main, 0).then(drawList);
        }
        drawList();
    }
    async function load() {
        if (loading)
            return loading;
        loading = (async () => {
            try {
                status = await notesApi.status();
                error = '';
            }
            catch (e) {
                const d = deniedOf(e);
                if (d)
                    status = d;
                else {
                    error = e instanceof Error ? e.message : String(e);
                }
            }
            draw();
        })().finally(() => { loading = null; });
        return loading;
    }
    /** before a write to the main note: the editor's pending autosave goes first (its text must not overwrite the line) */
    async function beforeWrite() {
        if (status?.key && files.activeId() === `${status.key}//${status.main}`)
            await files.flushNow();
    }
    /** after a write: the list and the folder key (a first note creates the folder), the open tab when it is clean */
    async function written(r) {
        await load();
        const ids = [r.name, typeof r.line === 'object' && r.line ? r.line.name : ''].filter(Boolean);
        for (const n of ids) {
            const ok = await files.reloadClean(`${r.key}//${n}`);
            if (!ok)
                deps.toast(`${n} has edits not saved yet - the new text is on disk; Files asks before it overwrites.`);
        }
    }
    async function appendLine(text) {
        try {
            await beforeWrite();
            const r = await notesApi.append(text);
            await written(r);
            return true;
        }
        catch (e) {
            const d = deniedOf(e);
            if (d) {
                status = d;
                draw();
            }
            else
                deps.toast(`Notes: ${e instanceof Error ? e.message : String(e)}`);
            return false;
        }
    }
    window.addEventListener('focus', () => { if (el.isConnected)
        void load(); });
    return {
        el,
        files,
        show(name) {
            if (!name)
                phoneFile = ''; // phone: More › Notes is the list
            // the keyboard would cover a phone's list: the field is focused on the desktop only
            void load().then(() => { if (name)
                openNote(name);
            else if (status?.state === 'ok' && !phone() && !document.activeElement?.closest('.notes'))
                capture.focus(); });
        },
        beforeWrite,
        written,
        status: () => status,
        load,
        dirty: () => files.dirty(),
        flush: () => files.flush(),
    };
}
//# sourceMappingURL=notes.js.map