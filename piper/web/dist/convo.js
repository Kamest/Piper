// Conversation view: the session's Claude Code or Codex transcript as
// readable text (piperd reads it on the host, secrets scrubbed), filterable, with the token
// usage. Search across sessions goes through /api/conversations/search.
import { api, ApiError } from './api.js';
import { clear, h } from './dom.js';
import { fileRefs } from './codenav.js';
import { showBidi, markBidi, matchRanges } from './text.js';
import { sessionDate } from './queue.js';
import { auditTime } from './board.js';
import { parseMarkdown, renderMarkdown } from './markdown.js';
import { references } from './references.js';
const REFRESH_MS = 5000;
function historyMarks(text, query) {
    const terms = query.match(/[\p{L}\p{N}]+/gu) ?? [];
    if (!terms.length)
        return [markBidi(text)];
    const rx = new RegExp(terms.join('|'), 'giu');
    const out = [];
    let at = 0;
    for (const m of text.matchAll(rx)) {
        out.push(markBidi(text.slice(at, m.index)), h('mark', {}, m[0]));
        at = m.index + m[0].length;
    }
    out.push(markBidi(text.slice(at)));
    return out;
}
/** R2: the hidden paths of a refused import (409 hidden_paths), null for any other error */
export function hiddenPaths(e) {
    if (!(e instanceof ApiError) || e.status !== 409 || !e.json || typeof e.json !== 'object')
        return null;
    const paths = e.json.hidden_paths;
    return Array.isArray(paths) ? paths.filter((x) => typeof x === 'string') : null;
}
/** R2: the confirmation inside the picker (a second modal would close it) - names only, never content */
export function hiddenImportNotice(paths, onImport, onCancel) {
    return h('div', { class: 'resume-hidden', role: 'alertdialog', 'aria-label': 'Transcript holds hidden file content' }, h('p', {}, h('b', {}, 'The transcript holds content of files hidden from Claude. '), 'Claude on the host read them; imported, the agent in the sandbox sees what the transcript keeps. Import anyway?'), paths.length ? h('ul', { class: 'st-warn' }, ...paths.map((p) => h('li', {}, h('code', {}, p))))
        : h('p', { class: 'small' }, 'The isolation rule cannot be read, so the hidden files cannot be named.'), h('div', { class: 'row' }, h('button', { onclick: onCancel }, 'Cancel'), h('button', { class: 'danger', onclick: onImport }, 'Import anyway')));
}
export function resumePicker(deps, initial = '') {
    const root = h('div', { class: 'resume-picker' });
    const search = h('input', { type: 'search', placeholder: 'Search topics, issue numbers or conversation text…',
        'aria-label': 'Search saved conversations', autocomplete: 'off', maxlength: '300' });
    const projectButton = h('button', { class: 'resume-project', 'aria-expanded': 'false', onclick: () => {
            const open = projectMenu.classList.toggle('hidden') === false;
            projectButton.setAttribute('aria-expanded', String(open));
            if (open) {
                projectSearch.value = '';
                drawProjects();
                projectSearch.focus();
            }
        } }, h('span', { class: 'rp-label' }, 'All projects'), h('i', { class: 'fi fi-chev-d', 'aria-hidden': 'true' }));
    const projectClear = h('button', { class: 'icon hidden', 'aria-label': 'Clear project filter', title: 'All projects', onclick: () => chooseProject('') }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }));
    const projectSearch = h('input', { type: 'search', placeholder: 'Find a project…', 'aria-label': 'Find a history project' });
    const projectOptions = h('div', { class: 'resume-project-options' });
    const projectMenu = h('div', { class: 'resume-project-menu hidden' }, projectSearch, projectOptions);
    const agent = h('select', { 'aria-label': 'Filter conversations by agent' }, h('option', { value: '' }, 'All agents'), h('option', { value: 'claude' }, 'Claude'), h('option', { value: 'codex' }, 'Codex'));
    const period = h('select', { 'aria-label': 'Filter conversations by date' }, h('option', { value: '' }, 'All time'), h('option', { value: 'today' }, 'Today'), h('option', { value: 'yesterday' }, 'Yesterday'), h('option', { value: 'week' }, 'Last 7 days'));
    const status = h('div', { class: 'resume-status mut small', role: 'status', 'aria-live': 'polite' });
    const external = h('input', { type: 'checkbox', 'aria-label': 'Include conversations outside Piper' });
    const target = h('select', { 'aria-label': 'Project for imported conversation' });
    const confirmCheckout = h('input', { type: 'checkbox', 'aria-label': 'Confirm another checkout' });
    const list = h('div', { class: 'resume-results', role: 'listbox', 'aria-label': 'Saved conversations', tabindex: '0' });
    const detail = h('div', { class: 'resume-detail', tabindex: '0', 'aria-label': 'Conversation preview' });
    const error = h('div', { class: 'bad small', role: 'alert' });
    const count = h('span', { class: 'mut small' });
    const resume = h('button', { class: 'primary', disabled: true, 'aria-label': 'Resume selected conversation', onclick: () => void launch(false) }, 'Resume ', h('kbd', {}, '⏎'));
    const fork = h('button', { disabled: true, 'aria-label': 'Fork selected conversation', onclick: () => void launch(true),
        title: 'Start a separate conversation with this history. Uses the same project checkout.' }, 'Fork conversation');
    const handOver = h('button', { class: 'hidden', onclick: () => { const id = selected; deps.closeModal(); deps.switchAgent?.(id); },
        title: 'Stops the session if it runs and hands its conversation to the other agent as a file.' }, 'Continue with…');
    const more = h('button', { class: 'link small hidden', onclick: () => { page += 50; void load(true); } }, 'Load more');
    const close = h('button', { class: 'icon', 'aria-label': 'Close conversation picker', title: 'Close · Esc', onclick: () => deps.closeModal() }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }));
    root.append(...(deps.mount ? [] : [h('div', { class: 'resume-head' }, h('h2', {}, 'Resume conversation'), close)]), h('div', { class: 'resume-filters' }, search, h('div', { class: 'resume-project-wrap' }, projectButton, projectClear, projectMenu), agent, period), h('label', { class: 'resume-external small' }, external, 'Include conversations outside Piper'), status, h('div', { class: 'resume-body' }, h('div', { class: 'resume-list' }, list, more), detail), error, h('div', { class: 'resume-footer' }, count, h('span', { class: 'grow' }), h('button', { onclick: () => { if (!deps.mount)
            deps.closeModal(); deps.newSession(project || rows.find((r) => r.session === selected)?.project || ''); }, title: 'Start with an empty conversation context' }, 'New conversation…'), handOver, fork, resume));
    let project = '';
    let projects = [];
    let targets = [];
    let rows = [];
    let selected = '';
    let page = 0;
    let generation = 0;
    let previewGeneration = 0;
    let timer = 0;
    let busy = false;
    let preview = null;
    let previewFor = '';
    let previewVersion = '';
    let loading = false;
    function drawProjects() {
        clear(projectOptions);
        projectOptions.append(h('button', { onclick: () => chooseProject('') }, 'All projects'));
        for (const p of projects.filter((p) => p.key.toLowerCase().includes(projectSearch.value.toLowerCase()))) {
            projectOptions.append(h('button', { onclick: () => chooseProject(p.key) }, p.key, h('span', { class: 'mut' }, String(p.count))));
        }
    }
    function chooseProject(value) {
        project = value;
        projectButton.querySelector('.rp-label').textContent = markBidi(value || 'All projects');
        projectClear.classList.toggle('hidden', !value);
        projectMenu.classList.add('hidden');
        projectButton.setAttribute('aria-expanded', 'false');
        search.focus();
        changed();
    }
    function changed() {
        page = 0;
        window.clearTimeout(timer);
        ++generation;
        loading = true;
        actions();
        status.textContent = 'Searching saved conversations…';
        timer = window.setTimeout(() => void load(), 180);
    }
    function actions() {
        const row = rows.find((r) => r.session === selected);
        const valid = previewFor === selected && preview?.available;
        const live = !!row && ['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting', 'ready', 'working', 'waiting', 'attention', 'stopping'].includes(row.state);
        const local = row?.source === 'local';
        const destination = targets.find(t => t.key === target.value);
        const importReady = !!destination && (destination.path === row?.cwd || confirmCheckout.checked);
        resume.disabled = busy || loading || !row || (!live && !valid) || (local && !importReady);
        fork.disabled = busy || loading || !row || !valid || local;
        const to = row && row.source === 'piper' && !row.closed ? deps.switchTo?.(row.session) ?? null : null;
        handOver.classList.toggle('hidden', !to);
        handOver.textContent = markBidi(`Continue with ${to ?? '…'}`);
        handOver.disabled = busy || loading || !valid;
        resume.replaceChildren(busy ? 'Opening…' : local ? 'Import and continue ' : live ? 'Open running session ' : 'Resume ', h('kbd', {}, '⏎'));
    }
    function drawDetail(row) {
        clear(detail);
        detail.append(h('h3', {}, row.title), h('p', { class: 'mut small' }, metadata(row)), h('p', { class: 'mut small' }, 'Last active: ' + sessionDate(row.activity, Date.now() / 1000)));
        if (row.import_from)
            detail.append(h('p', { class: 'mut small' }, 'Imported conversation · independent history'));
        else if (row.fork_from)
            detail.append(h('p', { class: 'mut small' }, 'Forked conversation · independent history'));
        if (row.source === 'local') {
            detail.append(h('p', { class: 'mut small' }, 'Outside Piper · ' + row.cwd), h('label', { class: 'small' }, 'Continue in project ', target));
            const destination = targets.find(t => t.key === target.value);
            if (destination && destination.path !== row.cwd)
                detail.append(h('p', { class: 'small' }, 'Target checkout: ' + destination.path), h('label', { class: 'resume-external small' }, confirmCheckout, 'Continue this history in the selected checkout'));
        }
        const text = (label, value) => {
            if (value)
                detail.append(h('h4', {}, label), h('pre', {}, ...historyMarks(value, search.value)));
        };
        text('Search match', row.snippet);
        text('Last answer', preview?.answer || row.answer);
        text('Last request', preview?.last || row.last);
        if (previewFor === row.session && preview) {
            if (!preview.available)
                detail.append(h('p', { class: 'bad small' }, preview.note || 'The saved conversation is unavailable. Start a new conversation explicitly.'));
            const transcript = h('details', {}, h('summary', {}, 'Recent conversation'));
            for (const m of preview.messages)
                transcript.append(h('div', { class: 'resume-message' }, h('div', { class: 'mut small' }, m.role === 'user' ? 'You' : row.agent === 'codex' ? 'Codex' : 'Claude'), h('pre', {}, ...historyMarks(m.text, search.value))));
            detail.append(transcript);
            text('First request', preview.first);
        }
        else
            detail.append(h('p', { class: 'mut small' }, 'Checking saved conversation…'));
        detail.append(h('p', { class: 'mut small' }, row.source === 'local' ?
            'Import copies this history into a separate Piper conversation. The original conversation stays available in your CLI.' :
            'Resume continues this context. Fork copies it to a separate conversation. Both use the same project checkout.'));
    }
    function metadata(row) {
        return [row.project || 'Unregistered project', row.branch, row.agent === 'codex' ? 'Codex' : 'Claude',
            row.created ? 'Created ' + sessionDate(row.created, Date.now() / 1000) : 'Creation date unknown'].filter(Boolean).join(' · ');
    }
    async function select(id) {
        if (id !== selected)
            clear(error); // R2: a hidden-files question belongs to the conversation it was asked for
        selected = id;
        preview = null;
        previewFor = '';
        const n = ++previewGeneration;
        const row = rows.find((r) => r.session === id);
        previewVersion = row ? JSON.stringify([row.messages, row.last, row.answer, row.title]) : '';
        for (const b of list.querySelectorAll('[data-session]'))
            b.setAttribute('aria-selected', String(b.dataset.session === id));
        list.querySelector(`[data-session="${id}"]`)?.scrollIntoView({ block: 'nearest' });
        actions();
        if (!row) {
            clear(detail);
            return;
        }
        target.replaceChildren(h('option', { value: '' }, 'Choose a project…'), ...targets.map(t => h('option', { value: t.key }, t.key)));
        target.value = row.project;
        confirmCheckout.checked = false;
        drawDetail(row);
        detail.scrollTop = 0;
        try {
            const r = await api.conversationPreview(id);
            if (n !== previewGeneration || !root.isConnected)
                return;
            preview = r.data;
            previewFor = id;
            drawDetail(row);
            actions();
        }
        catch (e) {
            if (n !== previewGeneration || !root.isConnected)
                return;
            detail.append(h('p', { class: 'bad small' }, String(e instanceof Error ? e.message : e)));
        }
    }
    async function load(append = false) {
        const n = ++generation;
        window.clearTimeout(timer);
        loading = true;
        actions();
        if (!rows.length)
            status.textContent = 'Loading saved conversations…';
        try {
            const r = await api.conversationHistory(search.value, project, agent.value, period.value, append ? page : 0, external.checked);
            if (n !== generation || !root.isConnected || !r.data)
                return;
            const data = r.data;
            projects = data.projects;
            targets = data.targets;
            rows = append ? [...rows, ...data.items] : data.items;
            if (!append)
                page = 0;
            clear(status);
            if (data.index.error)
                status.append(h('span', { class: 'bad' }, data.index.error));
            else if (data.index.busy)
                status.append(h('span', { class: 'spinner' }), markBidi(` Indexing history · ${data.index.done}/${data.index.total} conversations`), h('progress', { max: String(data.index.total || 1), value: String(data.index.done) }));
            else
                status.textContent = markBidi(`${data.total} conversation${data.total === 1 ? '' : 's'} · ↑↓ select · ⏎ resume · Esc close`);
            count.textContent = markBidi(`${rows.length} of ${data.total}`);
            more.classList.toggle('hidden', rows.length >= data.total);
            clear(list);
            for (const row of rows) {
                list.append(h('button', { class: 'resume-result', role: 'option', 'aria-selected': String(row.session === selected),
                    'data-session': row.session, tabindex: '-1', onclick: () => void select(row.session), ondblclick: () => void launch(false, row.session) }, h('span', { class: 'resume-title' }, ...historyMarks(row.title, search.value)), h('span', { class: 'mut small' }, metadata(row)), h('span', { class: 'mut small' }, `Last active: ${sessionDate(row.activity, Date.now() / 1000)}` + (row.source === 'local' ? ' · Outside Piper' : '')), h('span', { class: 'resume-snippet small' }, ...historyMarks(row.snippet || row.last || row.first || 'No conversation text yet', search.value)), row.closed || row.fork_from ? h('span', { class: 'mut small' }, [row.closed ? 'Closed in Piper' : '', row.fork_from ? 'Fork' : ''].filter(Boolean).join(' · ')) : null));
            }
            if (!rows.length)
                list.append(h('p', { class: 'mut' }, data.index.busy ? 'Searching while history is indexed…' : 'No matching conversations. Try another project or search.'));
            if (!rows.some((x) => x.session === selected)) {
                if (rows[0])
                    void select(rows[0].session);
                else {
                    selected = '';
                    preview = null;
                    previewFor = '';
                    ++previewGeneration;
                    clear(detail);
                    actions();
                }
            }
            else {
                const row = rows.find((x) => x.session === selected);
                if (!preview || !preview.available && row.available || previewVersion !== JSON.stringify([row.messages, row.last, row.answer, row.title]))
                    void select(selected);
                else if (previewFor === selected)
                    drawDetail(row); // a new search: this query's match and marks, not the last one's
                actions();
            }
            if (data.index.busy && !append)
                timer = window.setTimeout(() => void load(), 750);
        }
        catch (e) {
            if (n !== generation || !root.isConnected)
                return;
            status.textContent = markBidi(String(e instanceof Error ? e.message : e));
            status.classList.add('bad');
            status.append(' ', h('button', { class: 'mini', onclick: () => void load() }, 'Retry'));
        }
        finally {
            if (n === generation) {
                loading = false;
                actions();
            }
        }
    }
    async function launch(asFork, id = selected, confirmHidden = false) {
        if (id !== selected) {
            await select(id);
        }
        if (busy || !id || (asFork ? fork.disabled : resume.disabled))
            return;
        busy = true;
        clear(error);
        actions();
        try {
            const r = rows.find(r => r.session === id)?.source === 'local' ?
                await api.importConversation(id, target.value, confirmCheckout.checked, confirmHidden) : await api.resumeConversation(id, asFork);
            if (!r.data)
                throw new Error('No session was returned.');
            deps.closeModal();
            deps.opened(r.data.session);
        }
        catch (e) {
            const hidden = hiddenPaths(e);
            if (hidden)
                error.append(hiddenImportNotice(hidden, () => { clear(error); void launch(false, id, true); }, () => clear(error)));
            else
                error.textContent = markBidi(String(e instanceof Error ? e.message : e));
        }
        finally {
            busy = false;
            actions();
        }
    }
    search.addEventListener('input', changed);
    agent.addEventListener('change', changed);
    period.addEventListener('change', changed);
    external.addEventListener('change', changed);
    target.addEventListener('change', () => { confirmCheckout.checked = false; const row = rows.find(r => r.session === selected); if (row)
        drawDetail(row); actions(); });
    confirmCheckout.addEventListener('change', actions);
    projectSearch.addEventListener('input', drawProjects);
    root.addEventListener('keydown', (e) => {
        if (!projectMenu.classList.contains('hidden')) {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                projectMenu.classList.add('hidden');
                projectButton.setAttribute('aria-expanded', 'false');
                projectButton.focus();
            }
            return;
        }
        if (e.target !== search && e.target !== list && !e.target.closest('.resume-result'))
            return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            const at = rows.findIndex((r) => r.session === selected);
            const next = Math.min(rows.length - 1, Math.max(0, at + (e.key === 'ArrowDown' ? 1 : -1)));
            if (rows[next])
                void select(rows[next].session);
        }
        else if (e.key === 'Enter' && !resume.disabled) {
            e.preventDefault();
            void launch(false);
        }
    });
    if (deps.mount)
        deps.mount(root);
    else
        deps.modal('Resume conversation', root);
    search.focus();
    void load();
    if (initial)
        selected = initial;
}
export function fmtTokens(n) {
    return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n);
}
/** text with every (case-insensitive) occurrence of q wrapped in <mark> - no innerHTML */
export function marked(text, q) {
    if (!q)
        return [markBidi(text)];
    const out = [];
    let at = 0;
    for (const [i, j] of matchRanges(text, q)) {
        out.push(markBidi(text.slice(at, i)), h('mark', {}, text.slice(i, j)));
        at = j;
    }
    out.push(markBidi(text.slice(at)));
    return out;
}
/** the tool names of a turn that is only `[tool X]` markers, else null */
export function toolOnly(text) {
    const names = [...text.matchAll(/\[tool ([^\]]+)\]/g)].map((m) => m[1]);
    return names.length && !text.replace(/\[tool [^\]]+\]/g, '').trim() ? names : null;
}
/** text with its `path:line` references as links (the terminal's FILE_LINK) */
function linked(text, open) {
    const refs = open ? fileRefs(text) : [];
    if (!refs.length)
        return [showBidi(text)]; // Claude's / the user's text: a bidi control is a mark
    const out = [];
    let at = 0;
    for (const r of refs) {
        out.push(showBidi(text.slice(at, r.start)), h('button', { class: 'link', title: showBidi(`open ${r.path} at line ${r.line} in Piper's editor`),
            onclick: () => open(r.path, r.line) }, showBidi(text.slice(r.start, r.end))));
        at = r.end;
    }
    out.push(showBidi(text.slice(at)));
    return out;
}
/** F05: the transcript's token use in one human line - the exact numbers are the tooltip */
export function usageLine(u) {
    if (!u)
        return { text: '', title: '' };
    const total = u.input + u.output + u.cache_read + u.cache_write;
    const cache = total ? Math.floor(((u.cache_read + u.cache_write) / total) * 100) : 0;
    const n = (x) => x.toLocaleString('en-US');
    return {
        text: `${u.messages} ${u.messages === 1 ? 'answer' : 'answers'}${total ? ` · ${fmtTokens(total)} tokens (${cache} % cache)` : ''}`,
        title: `tokens from the transcript: input ${n(u.input)} · output ${n(u.output)} · cache read ${n(u.cache_read)} · cache write ${n(u.cache_write)}`,
    };
}
/** F05: how long a tool call ran and what it gave ("1.2 s · 3 lines"; "failed" first) */
export function toolMeta(t) {
    const ms = t.ms;
    const time = ms === undefined ? '' : ms < 10000 ? `${(ms / 1000).toFixed(1)} s` : ms < 60000 ? `${Math.round(ms / 1000)} s` : `${Math.round(ms / 60000)} min`;
    const lines = t.lines === undefined ? '' : `${t.lines} ${t.lines === 1 ? 'line' : 'lines'}`;
    return [t.error ? 'failed' : '', time, lines].filter(Boolean).join(' · ');
}
/** F05: a turn's text split at its `[tool X]` marker lines - text pieces (markdown) and the tool calls in order */
export function segments(text, tools = []) {
    const out = [];
    let buf = [];
    let k = 0;
    const flush = () => { if (buf.join('\n').trim())
        out.push({ md: buf.join('\n') }); buf = []; };
    for (const line of text.split('\n')) {
        const m = /^\s*\[tool ([^\]]+)\]\s*$/.exec(line);
        if (!m || m[1] === 'result') {
            buf.push(line);
            continue;
        }
        flush();
        const t = tools[k];
        out.push({ tool: t && t.name === m[1] ? t : { name: m[1], arg: '' } });
        if (t && t.name === m[1])
            k++;
    }
    flush();
    return out;
}
/** F05: the transcript's markdown rendered (markdown.ts, never innerHTML); its `path:line` references stay links */
function markdown(text, open = null, reference) {
    const root = renderMarkdown(parseMarkdown(showBidi(text)), { openRel: (href) => {
            const m = /^(.*?):(\d+)$/.exec(href);
            open?.(m ? m[1] : href, m ? Number(m[2]) : 1);
        } });
    if (reference)
        linkReferences(root, reference);
    else if (open)
        linkRefs(root, open);
    return root;
}
/** every text node's `path:line` (outside links and buttons) becomes a button into Piper's editor (K4) */
function linkRefs(el, open) {
    for (const c of [...el.childNodes]) {
        if (c.nodeType === 1) {
            if (!['A', 'BUTTON'].includes(c.tagName))
                linkRefs(c, open);
        }
        else if (c.nodeType === 3 && fileRefs(c.textContent ?? '').length) {
            const parts = linked(c.textContent ?? '', open);
            for (const x of parts)
                el.insertBefore(typeof x === 'string' ? document.createTextNode(markBidi(x)) : x, c);
            el.removeChild(c);
        }
    }
}
/** The transcript and terminal share reference recognition, including code / stacktrace text nodes. */
function linkReferences(el, open) {
    for (const child of [...el.childNodes]) {
        if (child.nodeType === 1 && !['A', 'BUTTON'].includes(child.tagName))
            linkReferences(child, open);
        else if (child.nodeType === 3) {
            const text = child.textContent ?? '', hits = references(text);
            if (!hits.length)
                continue;
            let at = 0;
            for (const hit of hits) {
                el.insertBefore(document.createTextNode(markBidi(text.slice(at, hit.start))), child);
                el.insertBefore(h('button', { class: 'link', title: `Open ${hit.text}`, onclick: () => open(hit.ref) }, hit.text), child);
                at = hit.end;
            }
            el.insertBefore(document.createTextNode(markBidi(text.slice(at))), child);
            el.removeChild(child);
        }
    }
}
export function createConvo(deps) {
    const el = h('div', { class: 'convo' });
    const cache = new Map();
    let current = '';
    let filter = '';
    let target = -1;
    let timer = 0;
    async function load(id, force = false) {
        const hit = cache.get(id);
        if (!force && hit && Date.now() - hit.at < REFRESH_MS)
            return;
        try {
            const r = await api.conversation(id);
            const prev = cache.get(id)?.data;
            if (r.data)
                cache.set(id, { at: Date.now(), data: r.data });
            // unchanged transcript: keep the DOM (text selection, scroll position)
            if (prev && r.data && prev.turns.length === r.data.turns.length
                && prev.turns[prev.turns.length - 1]?.text === r.data.turns[r.data.turns.length - 1]?.text)
                return;
        }
        catch (e) {
            // a session removed meanwhile (a reload restoring it, the poll not in yet) is no news: its tile goes with the next state
            if (!(e instanceof ApiError && e.status === 404))
                deps.toast(String(e instanceof Error ? e.message : e));
        }
        if (id === current)
            draw();
    }
    // persistent header: the filter keeps its focus and caret while the turns re-render
    // F05: the filter is this conversation's ⌘F; "Search all" (every conversation, snippets) is the search icon beside it
    const input = h('input', { type: 'search', placeholder: 'Filter transcript', autocomplete: 'off', 'aria-label': 'Filter this transcript (⌘F)' });
    input.addEventListener('input', () => { filter = input.value; draw(); });
    const usage = h('span', { class: 'mut cusage' });
    const turns = h('div', { class: 'turns' });
    const open = new Set(); // expanded tool rows (turn:index) - draw() rebuilds the DOM
    el.append(h('div', { class: 'chead' }, input, h('button', { class: 'icon csearch', 'aria-label': 'Search all conversations', title: 'Search all conversations', onclick: () => searchAll() }, h('i', { class: 'fi fi-search', 'aria-hidden': 'true' })), usage), turns);
    document.addEventListener('keydown', (e) => {
        const shown = el.isConnected && !el.classList.contains('hidden') && el.getClientRects().length > 0;
        const here = !document.activeElement || document.activeElement === document.body || el.contains(document.activeElement);
        if (!shown || !here || !(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== 'f')
            return;
        e.preventDefault();
        e.stopPropagation(); // capture: the global ⌘F (browser find here) never sees it
        input.focus();
        input.select();
    }, true);
    function toolRow(t, key) {
        const isOpen = open.has(key);
        const meta = toolMeta(t);
        const toggle = () => { if (!open.delete(key))
            open.add(key); draw(); };
        return h('div', { class: `trow${t.error ? ' failed' : ''}${isOpen ? ' open' : ''}`, role: 'button', tabindex: '0', 'aria-expanded': String(isOpen),
            title: isOpen ? 'fold' : 'show the whole call', onclick: toggle,
            onkeydown: (e) => { const k = e.key; if (k === 'Enter' || k === ' ') {
                e.preventDefault();
                toggle();
            } } }, h('span', { class: 'tcaret', 'aria-hidden': 'true' }), // a chevron mask, turned by .open
        h('span', { class: 'tname' }, showBidi(t.name)), h('span', { class: 'targ' }, showBidi(t.arg)), meta ? h('span', { class: 'tmeta' }, meta) : null);
    }
    function draw() {
        const atBottom = turns.scrollHeight - turns.scrollTop - turns.clientHeight < 40;
        const prev = turns.scrollTop;
        if (input.value !== filter)
            input.value = filter;
        clear(turns);
        const id = current;
        const opener = deps.openFile ? (path, line) => deps.openFile(id, path, line) : null;
        const c = cache.get(current)?.data;
        const u = c?.usage;
        const ul = usageLine(u ?? null);
        usage.textContent = markBidi(ul.text);
        usage.title = markBidi(ul.title);
        if (!c)
            turns.append(h('p', { class: 'mut' }, 'loading…'));
        else if (!c.turns.length)
            turns.append(h('p', { class: 'mut' }, c.note === undefined ? 'The transcript is empty so far.' : 'The transcript is not available yet (the session has not reported its ID yet).'));
        c?.turns.forEach((t, i) => {
            if (filter && !t.text.toLowerCase().includes(filter.toLowerCase()))
                return;
            if (filter) { // a match: the plain text with its marks
                turns.append(h('div', { class: `turn ${t.role}${i === target ? ' target' : ''}`, 'data-turn': String(i) }, who(t, c.agent), h('pre', {}, ...marked(t.text, filter))));
                return;
            }
            // F05: markdown rendered, each tool call a grey one-line row (a tool-only turn has no label of its own)
            const parts = segments(t.text, t.tools).map((x, k) => ('md' in x ? h('div', { class: 'tmd' }, markdown(x.md, opener, deps.onReference ? ref => deps.onReference(id, ref) : undefined)) : toolRow(x.tool, `${i}:${k}`)));
            const only = !!toolOnly(t.text) && i !== target;
            turns.append(h('div', { class: `turn ${t.role}${only ? ' tonly' : ''}${i === target ? ' target' : ''}`, 'data-turn': String(i) }, only ? null : who(t, c.agent), ...parts));
        });
        if (target >= 0 && c) {
            turns.querySelector(`[data-turn="${target}"]`)?.scrollIntoView({ block: 'center' });
            target = -1;
        }
        else
            turns.scrollTop = atBottom ? turns.scrollHeight : prev;
    }
    function who(t, agent) {
        return h('div', { class: 'who' }, t.role === 'user' ? 'You / task' : agent === 'codex' ? 'Codex' : 'Claude', t.ts ? h('span', { class: 'mut', title: new Date(t.ts).toLocaleString() }, ' ' + auditTime(t.ts)) : null);
    }
    function searchAll() {
        if (deps.resumeConversations) {
            deps.resumeConversations();
            return;
        }
        const input = h('input', { type: 'search', placeholder: 'Text in the conversations of all sessions, Enter', value: filter, autocomplete: 'off' });
        const list = h('div', { class: 'plist tall' });
        const run = async () => {
            clear(list);
            list.append(h('p', { class: 'mut' }, 'searching…'));
            let hits = [];
            try {
                hits = (await api.searchConversations(input.value)).data?.hits ?? [];
            }
            catch (e) {
                clear(list);
                list.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
                return;
            }
            clear(list);
            for (const x of hits) {
                list.append(h('button', { class: 'pitem col', onclick: () => { deps.closeModal(); filter = input.value; deps.jump(x.session, x.turn); } }, h('span', { class: 'name' }, `${x.title} · ${x.role === 'user' ? 'you' : 'Claude'}`), h('span', { class: 'mut snippet' }, ...marked(x.text, input.value))));
            }
            if (!hits.length)
                list.append(h('p', { class: 'mut' }, 'nothing found'));
        };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') {
            e.preventDefault();
            void run();
        } });
        deps.modal('Search conversations', input, list);
        input.focus();
    }
    return {
        el,
        searchAll,
        show(id, turn = -1) {
            if (id !== current) {
                current = id;
                filter = turn >= 0 ? filter : '';
            }
            target = turn;
            draw();
            void load(id, true);
            window.clearInterval(timer);
            timer = window.setInterval(() => { if (el.isConnected && !el.classList.contains('hidden'))
                void load(current); }, REFRESH_MS);
        },
        hide() {
            window.clearInterval(timer);
        },
    };
}
//# sourceMappingURL=convo.js.map