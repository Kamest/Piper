import { machineStorage, rawProjectKey } from './environment.js';
// The Git window: IntelliJ's Git tool window in one
// modal - branches and worktrees on the left, the log as a lane graph in the middle, the picked
// commit's files, details and a highlighted side-by-side / unified diff on the right. Push and History
// open over the window (W11 dialog layers: Esc goes back to it); New branch / New worktree / Delete branch
// stay sheets inside the window.
// Every repo string (subjects, authors, paths, branch names) is text only; h() marks bidi controls.
// Files hidden from Claude are shown (the GUI is the human's) and carry the eye mark.
import { api } from './api.js';
import { clear, fi, h, sideSplitter } from './dom.js';
import { createEditor, createMergeReview } from './cmeditor.js';
import { generatedNote, hiddenMark } from './gitpanel.js';
import { kbd } from './clipkeys.js';
import { markBidi } from './text.js';
import { laneCells, parseRefs, refChip, relAge } from './history.js';
// ---- pure helpers (node --test)
/** ↑2 ↓3 / ✓ (in sync) / local (no upstream) / gone (upstream deleted) */
export function branchMark(b) {
    if (b.gone)
        return 'gone';
    if (!b.upstream)
        return 'local';
    const parts = [b.ahead ? `↑${b.ahead}` : '', b.behind ? `↓${b.behind}` : ''].filter(Boolean);
    return parts.length ? parts.join(' ') : '✓';
}
/** remote branches grouped by remote, `<r>/HEAD` dropped, names without the remote prefix */
export function remoteGroups(remote) {
    const out = new Map();
    for (const b of remote) {
        const i = b.name.indexOf('/');
        if (i <= 0)
            continue;
        const r = b.name.slice(0, i);
        const name = b.name.slice(i + 1);
        if (name === 'HEAD')
            continue;
        if (!out.has(r))
            out.set(r, []);
        out.get(r).push({ name, full: b.name });
    }
    return out;
}
/** a long directory as `src/main/java/…/checkout` (first 3 segments + the last) */
export function shortDir(dir, keep = 3) {
    const p = dir.split('/');
    return p.length <= keep + 1 ? dir : [...p.slice(0, keep), '…', p[p.length - 1]].join('/');
}
/** a commit's files grouped by directory, directories and names sorted (the tree of the Files pane) */
export function fileTree(files) {
    const by = new Map();
    for (const f of files) {
        const i = f.path.lastIndexOf('/');
        const dir = i < 0 ? '' : f.path.slice(0, i);
        if (!by.has(dir))
            by.set(dir, []);
        by.get(dir).push(f);
    }
    const base = (p) => p.slice(p.lastIndexOf('/') + 1);
    return [...by.entries()].sort(([a], [b]) => a.localeCompare(b))
        .map(([dir, fs]) => ({ dir, files: fs.sort((a, b) => base(a.path).localeCompare(base(b.path))) }));
}
/** the files in the order the tree shows them (prev / next file walks this) */
export function treeOrder(files, tree) {
    return tree ? fileTree(files).flatMap((g) => g.files) : files;
}
/** git check-ref-format, roughly: what `git branch <name>` would refuse ('' = fine) */
export function branchNameProblem(name, taken = []) {
    if (!name)
        return 'Enter a name.';
    if (/[\s~^:?*[\\\x00-\x1f\x7f]/.test(name) || name.includes('..') || name.includes('@{') || name.includes('//')
        || name.startsWith('-') || name.startsWith('/') || name.endsWith('/') || name.endsWith('.') || name.endsWith('.lock')
        || name === '@' || name.split('/').some((s) => s.startsWith('.')))
        return 'Not a valid branch name.';
    for (const t of taken)
        if (t === name)
            return 'A branch of that name exists.';
    return '';
}
/** the name a worktree shows: `main checkout` or `@<dir name>` */
/** S3b: why the new-worktree sheet cannot create ('' = it can): the name first, then the branch
 *  (`existing` null = a new feat/<name>, '' = no free local branch to pick) */
export function newWorktreeProblem(name, existing, worktrees, local) {
    return worktreeNameProblem(name, worktrees)
        || (existing === null ? branchNameProblem(`feat/${name}`, local) : existing ? '' : 'No free local branch.');
}
export function worktreeLabel(w) {
    return w.main ? 'main checkout' : '@' + w.path.slice(w.path.replace(/\/+$/, '').lastIndexOf('/') + 1).replace(/\/+$/, '');
}
/** a new worktree's directory name (piper/projects.py WORKTREE_RX) and not taken ('' = fine) */
export function worktreeNameProblem(name, worktrees) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name))
        return 'Letters, digits, . _ - only (up to 64).';
    return worktrees.some((w) => worktreeLabel(w) === '@' + name) ? 'A worktree of that name exists.' : '';
}
/** a log row passes the text (subject / hash) and author filters */
export function rowMatches(r, text, author) {
    if (!r.hash)
        return !text && !author;
    if (author && r.author !== author)
        return false;
    if (!text)
        return true;
    const t = text.toLowerCase();
    return r.hash.startsWith(t) || (r.subject ?? '').toLowerCase().includes(t);
}
/** "+31 −8" / "+14" / "−1" / "" */
export function stat(add, del) {
    return [add ? `+${add}` : '', del ? `−${del}` : ''].filter(Boolean).join(' ');
}
const VIEW_KEY = 'piper.gitwindow.view';
const TREE_KEY = 'piper.gitwindow.tree';
function pref(key, fallback) {
    try {
        return machineStorage(window.localStorage).getItem(key) ?? fallback;
    }
    catch {
        return fallback;
    }
}
function setPref(key, value) {
    try {
        machineStorage(window.localStorage).setItem(key, value);
    }
    catch {
        /* private window: the choice lasts this window only */
    }
}
const errText = (e) => String(e instanceof Error ? e.message : e);
/** files as a tree or a flat list: one choice for the Git window, the Push dialog and the Commit panel (S2) */
export const treePref = () => pref(TREE_KEY, '1') === '1';
export const setTreePref = (on) => setPref(TREE_KEY, on ? '1' : '0');
function refChips(text) {
    return parseRefs(text).map(refChip);
}
/** F08: the toolbar's Push carries the current branch's unpushed commits (`↑ Push 2`), plain `Push` otherwise */
export function pushLabel(ahead) {
    return ahead && ahead > 0 ? `↑ Push ${ahead}` : 'Push';
}
// ---- the window
/** a read-only diff of one file: side by side (the merge view) or unified (the editor over its original) */
async function diffView(view, f, left, right) {
    if (view === 'side') {
        return createMergeReview({ path: f.path, ...(f.old_path ? { originalPath: f.old_path } : {}), left, right,
            onNote: () => { }, notes: () => new Map(), plain: true });
    }
    return createEditor({ path: f.path, text: right, original: left, readOnly: true, onDirty: () => { }, onCaret: () => { }, onSave: () => { },
        onDefinition: () => { }, onMenu: () => { }, onNote: () => { }, blame: () => null });
}
export function openGitWindow(deps, project) {
    let info = null;
    let rows = [];
    let ref = ''; // '' = all branches
    let commit = null;
    let file = null;
    let view = pref(VIEW_KEY, 'side') === 'unified' ? 'unified' : 'side';
    let tree = pref(TREE_KEY, '1') === '1';
    let shown = null;
    let seq = 0;
    const title = h('b', {}, 'Git');
    const where = h('span', { class: 'mut small' }, project);
    const branchChip = h('span', { class: 'gw-branch' });
    const pushBtn = deps.push ? h('button', { class: 'mini gw-push', title: 'push the session\'s branch (asks first)', onclick: () => deps.push(() => { if (win.isConnected) {
            void loadBranches();
            void loadRows();
        } }) }, 'Push') : null;
    const toolbar = h('div', { class: 'gw-tools' }, title, where, branchChip, h('span', { class: 'grow' }), h('button', { class: 'mini ghost gw-act', title: 'git fetch --all --prune', onclick: () => remote('fetch') }, h('span', { class: 'gw-arr', 'aria-hidden': 'true' }, '↓'), 'Fetch'), h('button', { class: 'mini ghost gw-act', title: 'git pull --ff-only (refused while a session works in this checkout)', onclick: () => remote('pull') }, h('span', { class: 'gw-arr', 'aria-hidden': 'true' }, '↓'), 'Pull'), pushBtn, h('button', { class: 'mini', title: 'close (Esc)', 'aria-label': 'close', onclick: deps.closeModal }, fi('x', 'gi')));
    const bfilter = h('input', { type: 'search', placeholder: 'Filter branches', autocomplete: 'off' });
    const blist = h('div', { class: 'gw-blist' });
    const side = h('div', { class: 'gw-side' }, bfilter, blist, h('div', { class: 'gw-foot' }, h('button', { class: 'mini', onclick: () => newBranch() }, '+ Branch'), h('button', { class: 'mini', onclick: () => newWorktree() }, '+ Worktree')));
    const search = h('input', { type: 'search', placeholder: 'Text or hash', autocomplete: 'off' });
    const refSel = h('select', { title: 'the branch whose log is shown' });
    const userSel = h('select', { title: 'author' });
    const list = h('div', { class: 'gw-rows', tabindex: 0 });
    const status = h('div', { class: 'gw-status mut small' });
    const log = h('div', { class: 'gw-log' }, h('div', { class: 'gw-filter' }, search, refSel, userSel), list, status);
    const filesHead = h('div', { class: 'gw-fhead' });
    const filesBox = h('div', { class: 'gw-files' });
    const details = h('div', { class: 'gw-details' }, h('p', { class: 'mut' }, 'Pick a commit.'));
    const diffHead = h('div', { class: 'gw-dhead' });
    const diffBox = h('div', { class: 'gw-diff' });
    const right = h('div', { class: 'gw-commit' }, h('div', { class: 'gw-top' }, h('div', { class: 'gw-fpane' }, filesHead, filesBox), details), diffHead, diffBox);
    const sheet = h('div', { class: 'gw-sheet hidden' });
    const columns = h('div', { class: 'gw-body' }, side, log, right);
    sideSplitter(columns, side, '--gw-side-w', 'piper.w.gw.side', 180, 400, 250, 'right', () => 440 + log.getBoundingClientRect().width);
    sideSplitter(columns, log, '--gw-log-w', 'piper.w.gw.log', 300, 700, 500, 'right', () => 440 + side.getBoundingClientRect().width);
    const win = h('div', { class: 'gw' }, toolbar, columns, sheet);
    deps.modal('Git', win);
    // ---- branches + worktrees
    async function loadBranches() {
        try {
            info = (await api.gitBranches(project)).data;
        }
        catch (e) {
            clear(blist);
            blist.append(h('p', { class: 'bad small' }, errText(e)));
            return;
        }
        drawBranches();
        drawRefSelect();
    }
    function drawBranches() {
        clear(blist);
        clear(branchChip);
        if (!info)
            return;
        const cur = info.local.find((b) => b.name === info.current);
        where.textContent = markBidi(rawProjectKey(project)); // S1: the project key, the path on hover
        where.title = markBidi(info.root);
        branchChip.append(h('span', { class: 'fi fi-branch' }), info.detached ? 'detached HEAD' : info.current, cur ? h('span', { class: 'mut' }, ' ' + branchMark(cur)) : '');
        if (pushBtn)
            pushBtn.textContent = markBidi(pushLabel(info.detached ? 0 : cur?.ahead));
        const q = bfilter.value.trim().toLowerCase();
        const hit = (n) => !q || n.toLowerCase().includes(q);
        const sec = (label, plus) => h('div', { class: 'gw-sec' }, label, h('span', { class: 'grow' }), plus ? h('button', { class: 'mini link', title: `new ${label.toLowerCase() === 'worktrees' ? 'worktree' : 'branch'}`, onclick: plus }, '+') : null);
        blist.append(sec('Local', () => newBranch()));
        for (const b of info.local.filter((x) => hit(x.name))) {
            const current = b.name === info.current && !info.detached;
            const wt = b.worktree && info.worktrees.find((w) => w.path === b.worktree && !w.main);
            blist.append(h('div', { class: 'gw-b' + (current ? ' cur' : '') + (ref === b.name ? ' on' : '') }, h('button', { class: 'gw-bn', title: `${b.name} · ${b.subject}\nclick: its log`, onclick: () => pickRef(b.name) }, h('span', { class: 'fi fi-branch' }), h('span', { class: 'nm' }, b.name)), wt ? h('span', { class: 'pill', title: b.worktree ?? '' }, worktreeLabel(wt)) : null, h('span', { class: 'gw-mark mut small' }, branchMark(b)), current || b.worktree ? null : h('span', { class: 'gw-acts' }, h('button', { class: 'mini', title: `switch this checkout to ${b.name}`, onclick: () => switchTo(b.name) }, 'Switch'), h('button', { class: 'mini', title: `delete ${b.name}`, 'aria-label': `delete ${b.name}`, onclick: () => deleteBranch(b.name, false) }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })))));
        }
        const groups = remoteGroups(info.remote);
        if (groups.size)
            blist.append(sec('Remote'));
        for (const [r, bs] of groups) {
            blist.append(h('div', { class: 'gw-rname mut small' }, r));
            for (const b of bs.filter((x) => hit(x.full))) {
                const local = info.local.some((l) => l.name === b.name);
                blist.append(h('div', { class: 'gw-b remote' + (ref === b.full ? ' on' : '') }, h('button', { class: 'gw-bn', title: `${b.full}\nclick: its log`, onclick: () => pickRef(b.full) }, h('span', { class: 'fi fi-cloud' }), h('span', { class: 'nm' }, b.name)), local ? null : h('span', { class: 'gw-acts' }, h('button', { class: 'mini', title: `a local ${b.name} tracking ${b.full}, checked out here`, onclick: () => checkoutRemote(b.name, b.full) }, 'Checkout'))));
            }
        }
        blist.append(sec('Worktrees', () => newWorktree()));
        for (const w of info.worktrees) {
            const live = w.sessions.length;
            blist.append(h('div', { class: 'gw-wt' }, h('div', { class: 'gw-wtop' }, h('span', { class: 'fi fi-dir' }), h('span', { class: 'nm', title: w.path }, worktreeLabel(w)), h('span', { class: 'grow' }), live ? h('button', { class: 'pill', title: 'open the session', onclick: () => openSession(w.sessions[0]) }, live === 1 ? '1 session' : `${live} sessions`) : null, !w.main && !live && w.key ? h('button', { class: 'mini gw-acts', title: `remove the worktree ${w.path} (the branch stays)`, 'aria-label': 'remove worktree', onclick: () => removeWorktree(w) }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })) : null), h('div', { class: 'gw-wsub mut small' }, w.branch || (w.head ? w.head.slice(0, 7) + ' (detached)' : ''), w.main ? ` · ${project}` : '', h('span', { class: 'grow' }), !live && w.key && deps.newSession ? h('button', { class: 'link small', onclick: () => { deps.closeModal(); deps.newSession(w.key); } }, 'Open session') : null)));
        }
    }
    function drawRefSelect() {
        clear(refSel);
        refSel.append(h('option', { value: '' }, 'Branch: All'));
        for (const b of info?.local ?? [])
            refSel.append(h('option', { value: b.name }, b.name));
        for (const b of info?.remote ?? [])
            if (!b.name.endsWith('/HEAD'))
                refSel.append(h('option', { value: b.name }, b.name));
        refSel.value = ref;
    }
    function openSession(id) {
        deps.closeModal();
        deps.openSession?.(id);
    }
    function pickRef(name) {
        ref = ref === name ? '' : name;
        refSel.value = ref;
        drawBranches();
        void loadRows();
    }
    async function op(what, run, ok) {
        try {
            const r = await run();
            const msg = ok(r);
            if (msg)
                deps.toast(msg);
            await Promise.all([loadBranches(), loadRows()]);
            return r;
        }
        catch (e) {
            deps.toast(`${what} failed: ${errText(e)}`);
            return null;
        }
    }
    function remote(action) {
        deps.toast(action === 'fetch' ? 'Fetching…' : 'Pulling…');
        void op(action === 'fetch' ? 'Fetch' : 'Pull', async () => (await api.gitRemote(project, action)).data, (r) => (r?.output?.trim() ? r.output.trim().split('\n').slice(-1)[0] : action === 'fetch' ? 'Fetched' : 'Pulled'));
    }
    function switchTo(name) {
        void op('Switch', async () => (await api.gitBranch(project, { action: 'switch', name })).data, () => `On ${name}`);
    }
    function checkoutRemote(name, full) {
        void op('Checkout', async () => (await api.gitBranch(project, { action: 'create', name, start: full, checkout: true })).data, () => `On ${name} (tracks ${full})`);
    }
    async function deleteBranch(name, force) {
        const r = await op('Delete', async () => (await api.gitBranch(project, { action: 'delete', name, force })).data, (x) => (x?.deleted ? `Deleted ${name}` : undefined));
        if (r && !r.deleted && r.unmerged?.length)
            confirmDelete(name, r.unmerged);
    }
    // ---- sheets (small dialogs of the window live inside it)
    function openSheet(head, body, foot) {
        clear(sheet);
        sheet.append(h('div', { class: 'gw-card', role: 'dialog', 'aria-label': head }, h('div', { class: 'gw-chead' }, h('b', {}, head), h('span', { class: 'grow' }), h('button', { class: 'mini', title: 'close', 'aria-label': 'close', onclick: closeSheet }, fi('x', 'gi'))), h('div', { class: 'gw-cbody' }, ...body), h('div', { class: 'row' }, ...foot)));
        sheet.classList.remove('hidden');
        sheet.querySelector('input, button.primary')?.focus();
    }
    function closeSheet() {
        sheet.classList.add('hidden');
        clear(sheet);
        list.focus({ preventScroll: true });
    }
    sheet.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            closeSheet();
        }
    });
    function startSelect() {
        const sel = h('select', {});
        const headSubj = info?.local.find((b) => b.name === info?.current);
        sel.append(h('option', { value: '' }, `HEAD · ${info?.detached ? 'detached' : info?.current ?? ''}${headSubj ? ` (${headSubj.head.slice(0, 7)})` : ''}`));
        for (const b of info?.local ?? [])
            if (b.name !== info?.current)
                sel.append(h('option', { value: b.name }, b.name));
        for (const b of info?.remote ?? [])
            if (!b.name.endsWith('/HEAD'))
                sel.append(h('option', { value: b.name }, b.name));
        if (commit)
            sel.append(h('option', { value: commit.hash }, `${commit.short} · the selected commit`));
        return sel;
    }
    function newBranch() {
        const name = h('input', { type: 'text', placeholder: 'feat/1950-pay-later', autocomplete: 'off', spellcheck: 'false' });
        const hint = h('p', { class: 'mut small' }, ' ');
        const start = startSelect();
        const sw = h('input', { type: 'checkbox', checked: true });
        const create = h('button', { class: 'primary', disabled: true }, 'Create');
        const check = () => {
            const p = branchNameProblem(name.value.trim(), (info?.local ?? []).map((b) => b.name));
            hint.textContent = markBidi(name.value.trim() ? p || 'Valid name, not taken.' : ' ');
            hint.className = 'small ' + (p ? 'bad' : 'mut');
            create.disabled = !!p;
        };
        name.addEventListener('input', check);
        const go = () => {
            if (create.disabled)
                return;
            const n = name.value.trim();
            create.disabled = true;
            void op('New branch', async () => (await api.gitBranch(project, { action: 'create', name: n, start: start.value, checkout: sw.checked })).data, () => (sw.checked ? `On ${n}` : `Created ${n}`)).then((r) => { if (r)
                closeSheet();
            else
                create.disabled = false; });
        };
        name.addEventListener('keydown', (e) => { if (e.key === 'Enter') {
            e.preventDefault();
            go();
        } });
        create.addEventListener('click', go);
        openSheet('New branch', [
            h('label', { class: 'gw-field' }, h('span', {}, 'Name'), name), hint,
            h('label', { class: 'gw-field' }, h('span', {}, 'Start from'), start),
            h('label', { class: 'row left' }, sw, 'Switch to it'),
            h('p', { class: 'mut small' }, 'Switching is refused while this checkout has uncommitted changes or a session is running in it.'),
        ], [h('button', { onclick: closeSheet }, 'Cancel'), create]);
    }
    function newWorktree() {
        const name = h('input', { type: 'text', placeholder: 'pay-later', autocomplete: 'off', spellcheck: 'false' });
        const isNew = h('input', { type: 'radio', name: 'gw-wtb', checked: true });
        const isOld = h('input', { type: 'radio', name: 'gw-wtb' });
        const free = (info?.local ?? []).filter((b) => !b.worktree && b.name !== info?.current);
        const existing = h('select', {});
        for (const b of free)
            existing.append(h('option', { value: b.name }, b.name));
        if (!free.length) {
            isOld.disabled = true;
            existing.disabled = true;
        }
        const newHint = h('span', { class: 'mut small' });
        const path = h('code', { class: 'small' });
        const key = h('p', { class: 'mut small' });
        const hint = h('p', { class: 'small bad' });
        const startIt = h('input', { type: 'checkbox', checked: !!deps.newSession, disabled: !deps.newSession });
        const create = h('button', { class: 'primary', disabled: true }, 'Create worktree');
        const base = info?.base ?? rawProjectKey(project).split('@')[0];
        const check = () => {
            const n = name.value.trim();
            newHint.textContent = markBidi(`feat/${n || '<name>'} from HEAD`);
            path.textContent = markBidi(`${rawProjectKey(project).split('@')[0]}/.claude/worktrees/${n || '<name>'}`); // S3b: project-relative
            path.title = markBidi(`${info?.root ?? ''}/.claude/worktrees/${n}`);
            key.textContent = markBidi(`Session project: ${base}@${n || '<name>'}`);
            const bad = newWorktreeProblem(n, isNew.checked ? null : existing.value, info?.worktrees ?? [], (info?.local ?? []).map((b) => b.name));
            hint.textContent = markBidi(n ? bad : '');
            create.disabled = !!bad;
        };
        for (const x of [name, isNew, isOld, existing])
            x.addEventListener('input', check);
        check();
        const go = () => {
            if (create.disabled)
                return;
            const n = name.value.trim();
            create.disabled = true;
            const body = isNew.checked ? { action: 'add', name: n, branch: `feat/${n}`, new: true } : { action: 'add', name: n, branch: existing.value };
            void op('New worktree', async () => (await api.gitWorktree(project, body)).data, (r) => `Worktree ${r?.key ?? n} on ${r?.branch ?? ''}`).then((r) => {
                if (!r) {
                    create.disabled = false;
                    return;
                }
                closeSheet();
                if (startIt.checked && r.key && deps.newSession) {
                    deps.closeModal();
                    deps.newSession(r.key);
                }
            });
        };
        name.addEventListener('keydown', (e) => { if (e.key === 'Enter') {
            e.preventDefault();
            go();
        } });
        create.addEventListener('click', go);
        openSheet('New worktree', [
            h('label', { class: 'gw-field' }, h('span', {}, 'Name'), name), hint,
            h('div', { class: 'gw-field' }, h('span', {}, 'Branch'), h('label', { class: 'row left' }, isNew, 'New branch', newHint), h('label', { class: 'row left' }, isOld, 'Existing', existing)),
            h('div', { class: 'gw-field gw-tile' }, h('span', {}, 'Path'), path, key),
            h('label', { class: 'row left' }, startIt, 'Start a session in it'),
        ], [h('button', { onclick: closeSheet }, 'Cancel'), create]);
    }
    function confirmDelete(name, unmerged) {
        const n = unmerged.length;
        openSheet('Delete branch', [
            h('p', { class: 'gw-danger' }, h('i', { class: 'fi fi-alert', 'aria-hidden': 'true' }), h('span', {}, `${name} has ${n === 1 ? '1 commit that is' : `${n} commits that are`} not in HEAD or its upstream. ${n === 1 ? 'It' : 'They'} will be lost.`)),
            h('div', { class: 'gw-lost' }, ...unmerged.slice(0, 20).map((l) => {
                const i = l.indexOf(' ');
                return h('div', {}, h('code', {}, i < 0 ? l : l.slice(0, i)), ' ', i < 0 ? '' : l.slice(i + 1));
            }), n > 20 ? h('p', { class: 'mut small' }, `… and ${n - 20} more`) : null),
        ], [h('button', { onclick: closeSheet }, 'Cancel'),
            h('button', { class: 'danger', onclick: () => { closeSheet(); void deleteBranch(name, true); } }, 'Delete anyway')]);
    }
    function removeWorktree(w) {
        const name = worktreeLabel(w).slice(1);
        openSheet('Remove worktree', [
            h('p', {}, `Remove ${w.path}? The branch ${w.branch || '(detached)'} stays; uncommitted changes there make git refuse.`),
        ], [h('button', { onclick: closeSheet }, 'Cancel'),
            h('button', { class: 'danger', onclick: () => { closeSheet(); void op('Remove worktree', async () => (await api.gitWorktree(project, { action: 'remove', name })).data, () => `Removed ${w.path}`); } }, 'Remove')]);
    }
    // ---- log
    let shownRows = [];
    let at = -1;
    let timer = 0;
    async function loadRows() {
        const mine = ++seq;
        status.textContent = 'loading…';
        try {
            const r = (await api.gitRows(project, 300, ref)).data;
            if (mine !== seq)
                return;
            rows = r?.rows ?? [];
        }
        catch (e) {
            if (mine !== seq)
                return;
            clear(list);
            list.append(h('p', { class: 'bad' }, errText(e)));
            status.textContent = '';
            return;
        }
        const authors = [...new Set(rows.map((x) => x.author).filter((x) => !!x))].sort();
        const was = userSel.value;
        clear(userSel);
        userSel.append(h('option', { value: '' }, 'User: All'), ...authors.map((a) => h('option', { value: a }, a)));
        userSel.value = authors.includes(was) ? was : '';
        drawRows();
    }
    function drawRows() {
        const keep = shownRows[at]?.row.hash;
        clear(list);
        shownRows = [];
        at = -1;
        const text = search.value.trim();
        const user = userSel.value;
        const filtered = !!(text || user);
        const now = Date.now() / 1000;
        for (const r of rows) {
            if (!rowMatches(r, text, user))
                continue;
            // a filtered log has gaps: the lanes would lie, so they are left out
            const lanes = filtered ? [] : laneCells(r.graph).map((x) => h('span', { class: `gc ${x.kind}` + (x.lane < 0 ? '' : ` lane l${x.lane}`) }, x.t));
            if (!r.hash) {
                list.append(h('div', { class: 'gw-row lane-only' }, h('span', { class: 'gw-g' }, ...lanes)));
                continue;
            }
            const el = h('button', { class: 'gw-row', title: `${r.hash}\n${r.author ?? ''} · ${r.date ?? ''}` }, h('span', { class: 'gw-g' }, ...lanes), h('span', { class: 'gw-subj' }, ...(r.refs ? refChips(r.refs) : []), h('span', { class: 's' }, r.subject ?? '')), h('span', { class: 'gw-au mut' }, r.author ?? ''), h('span', { class: 'gw-age mut' }, r.date ? relAge(Date.parse(r.date) / 1000, now) : ''), h('code', { class: 'gw-h mut' }, r.short ?? r.hash.slice(0, 7)));
            const i = shownRows.length;
            el.addEventListener('click', () => { select(i); list.focus({ preventScroll: true }); });
            list.append(el);
            shownRows.push({ el, row: r });
        }
        const n = shownRows.length;
        status.textContent = markBidi(`${n} commit${n === 1 ? '' : 's'} · ${ref || 'all branches'} · ↑↓ walk · Enter opens the diff`);
        if (!n)
            list.append(h('p', { class: 'mut' }, rows.length ? 'Nothing matches the filter.' : 'No commits.'));
        const back = keep ? shownRows.findIndex((x) => x.row.hash === keep) : -1;
        if (back >= 0)
            select(back, false, false);
        else if (n)
            select(0);
    }
    function select(i, now = true, load = true) {
        if (i < 0 || i >= shownRows.length)
            return;
        shownRows[at]?.el.classList.remove('active');
        at = i;
        const r = shownRows[i];
        r.el.classList.add('active');
        r.el.scrollIntoView({ block: 'nearest' });
        if (!load)
            return;
        window.clearTimeout(timer);
        // walking the log with the keyboard fetches only where it stops
        if (now)
            void loadCommit(r.row.hash);
        else
            timer = window.setTimeout(() => void loadCommit(r.row.hash), 180);
    }
    list.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            select(Math.max(0, Math.min(shownRows.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1))), false);
        }
        else if (e.key === 'Enter') {
            e.preventDefault();
            shown?.focus();
        }
    });
    let typing = 0;
    search.addEventListener('input', () => { window.clearTimeout(typing); typing = window.setTimeout(drawRows, 150); });
    userSel.addEventListener('change', drawRows);
    refSel.addEventListener('change', () => { ref = refSel.value; drawBranches(); void loadRows(); });
    bfilter.addEventListener('input', drawBranches);
    // ---- one commit
    let cseq = 0;
    async function loadCommit(hash) {
        const mine = ++cseq;
        try {
            const c = (await api.gitCommitDetail(project, hash)).data;
            if (mine !== cseq || !c)
                return;
            commit = c;
        }
        catch (e) {
            if (mine !== cseq)
                return;
            clear(details);
            details.append(h('p', { class: 'bad' }, errText(e)));
            return;
        }
        drawCommit();
        const first = treeOrder(commit.files, tree)[0];
        if (first)
            void showFile(first);
        else {
            file = null;
            drawDiffHead();
            destroyShown();
            clear(diffBox);
            diffBox.append(h('p', { class: 'mut' }, 'No file changes (a merge shows its first-parent diff only when it has one).'));
        }
    }
    function drawCommit() {
        if (!commit)
            return;
        const c = commit;
        const [subject, ...rest] = c.message.split('\n');
        const body = rest.join('\n').trim();
        const local = info?.local.find((b) => b.head && c.hash.startsWith(b.head));
        const inBranch = (parseRefs(c.refs).find((r) => r.kind === 'head' || r.kind === 'local')?.name) ?? local?.name ?? '';
        clear(details);
        details.append(h('b', { class: 'gw-subject' }, subject ?? ''), h('p', { class: 'gw-body-text', hidden: !body }, body), h('div', { class: 'gw-meta small' }, h('span', { class: 'mut' }, 'Commit'), h('span', {}, h('code', {}, c.short), ' · ', h('button', { class: 'link small', onclick: () => void navigator.clipboard?.writeText(c.hash).then(() => deps.toast('Copied')) }, 'copy')), h('span', { class: 'mut' }, 'Author'), h('span', {}, `${c.author} <${c.email}> · ${new Date(c.date).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short', hourCycle: 'h23' })}`), h('span', { class: 'mut' }, c.parents.length > 1 ? 'Parents' : 'Parent'), h('span', {}, ...(c.parents.length ? c.parents.flatMap((p, i) => [i ? ', ' : '', h('button', { class: 'link small', onclick: () => jump(p) }, p.slice(0, 7))]) : ['(root)'])), inBranch ? h('span', { class: 'mut' }, 'In') : null, inBranch ? h('span', {}, inBranch) : null));
        drawFiles();
    }
    function jump(hash) {
        const i = shownRows.findIndex((x) => x.row.hash === hash);
        if (i >= 0) {
            select(i);
            return;
        }
        void loadCommit(hash);
    }
    function drawFiles() {
        clear(filesHead);
        clear(filesBox);
        if (!commit)
            return;
        const files = commit.files;
        const add = files.reduce((s, f) => s + f.add, 0);
        const del = files.reduce((s, f) => s + f.del, 0);
        filesHead.append(h('b', {}, `${files.length} file${files.length === 1 ? '' : 's'}`), h('span', { class: 'gw-stat' }, stat(add, del)), h('span', { class: 'grow' }), h('button', { class: 'mini' + (tree ? ' on' : ''), title: 'group by directory', 'aria-pressed': String(tree), onclick: () => { tree = true; setPref(TREE_KEY, '1'); drawFiles(); } }, 'Tree'), h('button', { class: 'mini' + (!tree ? ' on' : ''), title: 'flat list', 'aria-pressed': String(!tree), onclick: () => { tree = false; setPref(TREE_KEY, '0'); drawFiles(); } }, 'List'));
        const row = (f, full) => {
            const name = full ? f.path : f.path.slice(f.path.lastIndexOf('/') + 1);
            const old = f.old_path ? (full ? f.old_path : f.old_path.slice(f.old_path.lastIndexOf('/') + 1)) : '';
            return h('button', { class: 'gw-f' + (f === file ? ' active' : '') + (full ? '' : ' in'), title: f.old_path ? `${f.old_path} → ${f.path}` : f.path, onclick: () => void showFile(f) }, h('span', { class: `gw-st st-${f.status[0]}` }, f.status[0] ?? ''), h('span', { class: 'nm' }, old ? `${name} ← ${old}` : name), h('span', { class: 'grow' }), h('span', { class: 'gw-stat small' }, f.binary ? 'bin' : stat(f.add, f.del)), f.withheld ? hiddenMark() : null);
        };
        if (tree) {
            for (const g of fileTree(files)) {
                if (g.dir)
                    filesBox.append(h('div', { class: 'gw-dir mut small', title: g.dir }, h('span', { class: 'fi fi-dir' }), shortDir(g.dir)));
                for (const f of g.files)
                    filesBox.append(row(f, false));
            }
        }
        else
            for (const f of files)
                filesBox.append(row(f, true));
        if (commit.truncated)
            filesBox.append(h('p', { class: 'mut small' }, 'More files than shown.'));
    }
    function destroyShown() {
        shown?.destroy();
        shown = null;
    }
    function drawDiffHead() {
        clear(diffHead);
        if (!file || !commit)
            return;
        const f = file;
        const order = treeOrder(commit.files, tree);
        const i = order.indexOf(f);
        const seg = (v, label) => h('button', {
            class: 'mini' + (view === v ? ' on' : ''), 'aria-pressed': String(view === v),
            onclick: () => { if (view === v)
                return; view = v; setPref(VIEW_KEY, v); void showFile(f); },
        }, label);
        diffHead.append(h('b', { class: 'nm', title: f.path }, f.path.slice(f.path.lastIndexOf('/') + 1)), h('span', { class: 'gw-stat small' }, stat(f.add, f.del)), ...(f.withheld ? [hiddenMark()] : []), h('span', { class: 'grow' }), h('span', { class: 'seg' }, seg('side', 'Side by side'), seg('unified', 'Unified')), h('button', { class: 'mini', title: 'previous file', 'aria-label': 'previous file', disabled: i <= 0, onclick: () => void showFile(order[i - 1]) }, '↑'), h('button', { class: 'mini', title: 'next file', 'aria-label': 'next file', disabled: i < 0 || i >= order.length - 1, onclick: () => void showFile(order[i + 1]) }, '↓'), ...(deps.openFile && f.status[0] !== 'D' ? [h('button', { class: 'mini', title: 'the file as it is now, in Piper\'s editor', onclick: () => { deps.closeModal(); deps.openFile(project, f.path, 0); } }, 'Open file')] : []), ...(deps.history ? [h('button', { class: 'mini', title: 'the file\'s history', onclick: () => deps.history(project, f.path) }, 'History')] : []));
    }
    let fseq = 0;
    async function showFile(f) {
        if (!commit)
            return;
        const c = commit;
        const mine = ++fseq;
        file = f;
        drawFiles();
        drawDiffHead();
        destroyShown();
        clear(diffBox);
        if (f.binary)
            return void diffBox.append(h('p', { class: 'mut' }, 'Binary file - no text diff.'));
        diffBox.append(h('p', { class: 'mut' }, 'loading…'));
        let left = '';
        let rightText = '';
        try {
            const st = f.status[0];
            const [o, n] = await Promise.all([
                st === 'A' ? null : api.gitBlob(project, c.hash, f.old_path ?? f.path, 'old'),
                st === 'D' ? null : api.gitBlob(project, c.hash, f.path, 'new'),
            ]);
            left = o?.data?.text ?? '';
            rightText = n?.data?.text ?? '';
        }
        catch (e) {
            if (mine !== fseq)
                return;
            clear(diffBox);
            diffBox.append(h('p', { class: 'bad' }, errText(e)));
            return;
        }
        if (mine !== fseq)
            return;
        clear(diffBox);
        const d = await diffView(view, f, left, rightText);
        if (mine !== fseq)
            return void d.destroy();
        shown = d;
        diffBox.append(d.dom);
    }
    // the window lives as long as the overlay holds it: a closed window frees its editors
    const gone = new MutationObserver(() => {
        if (win.isConnected)
            return;
        gone.disconnect();
        destroyShown();
    });
    // #overlay: a layer over it or this layer going away (a replaced top layer leaves its parent untouched)
    const host = win.closest('#overlay') ?? win.parentElement?.parentElement;
    if (host)
        gone.observe(host, { childList: true });
    void loadBranches();
    void loadRows();
    list.focus({ preventScroll: true });
}
// ---- S5: the Push dialog (IntelliJ's Push Commits): where it goes, what the checks found, the outgoing
// commits on the left ("All" = their files together), the picked commit's files on the right, the file's diff below.
/** the Push button: disabled by a block check, "Push 2 commits", or "Push" for a new branch without new commits */
export function pushButton(p) {
    const n = p.commits.length;
    return { label: n ? `Push ${n}${p.more ? '+' : ''} ${n === 1 && !p.more ? 'commit' : 'commits'}` : 'Push',
        disabled: p.checks.some((c) => c.level === 'block') };
}
/** F27: the dialog's one button - "Push & open MR" while the MR is ticked */
export function pushGoLabel(p, mr, gh) {
    const n = p.commits.length;
    return mr ? `Push & open ${gh ? 'PR' : 'MR'}` : `Push ${n ? `${n}${p.more ? '+' : ''} ${n === 1 && !p.more ? 'commit' : 'commits'}` : ''}`.trim();
}
export function openPushDialog(deps) {
    const { project } = deps;
    let p = null;
    let pick = -1; // -1 = all commits
    let files = [];
    let file = null;
    let view = pref(VIEW_KEY, 'side') === 'unified' ? 'unified' : 'side';
    let tree = pref(TREE_KEY, '1') === '1';
    let shown = null;
    let cseq = 0;
    let fseq = 0;
    const title = h('b', { class: 'pd-title' }, 'Push');
    const target = h('div', { class: 'pd-target' }, h('span', { class: 'mut' }, 'loading…'));
    const checks = h('div', { class: 'pd-checks' });
    const clist = h('div', { class: 'pd-commits' });
    const fhead = h('div', { class: 'gw-fhead pd-phead' });
    const flist = h('div', { class: 'gw-files' });
    const dhead = h('div', { class: 'gw-dhead' });
    const dbox = h('div', { class: 'gw-diff' });
    const cmd = h('code', { class: 'mut small pd-cmd' });
    const goLabel = h('span', {}, 'Push');
    const go = h('button', { class: 'primary', disabled: true }, goLabel, h('kbd', {}, kbd('⌘⏎', 'Ctrl')));
    // F27: the commits stay in sight, their files and diff fold behind one line
    const brief = h('div', { class: 'pd-brief' });
    const showDiff = h('button', { class: 'link small pd-show', 'aria-expanded': 'false' }, 'show diff');
    const detail = h('div', { class: 'pd-detail hidden' }, h('div', { class: 'pd-body' }, h('div', { class: 'pd-pane' }, h('div', { class: 'gw-fhead pd-phead' }, h('b', {}, 'Commits')), clist), h('div', { class: 'pd-pane' }, fhead, flist)), dhead, dbox);
    const mrBox = deps.mr ? mrSection(deps.mr) : null;
    const win = h('div', { class: 'pd' }, h('div', { class: 'pd-head' }, h('div', { class: 'pd-hrow' }, title, h('span', { class: 'grow' }), h('button', { class: 'icon pd-x', title: 'close (Esc)', 'aria-label': 'close', onclick: deps.closeModal }, fi('x', 'gi'))), target), checks, brief, detail, mrBox?.el ?? null, h('div', { class: 'pd-foot' }, cmd, h('span', { class: 'grow' }), h('button', { onclick: deps.closeModal }, 'Cancel', h('kbd', {}, 'esc')), go));
    go.onclick = () => {
        const mr = mrBox?.values() ?? null;
        deps.closeModal();
        deps.push(mr);
    };
    win.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !go.disabled) {
            e.preventDefault();
            e.stopPropagation();
            go.click();
        }
    });
    showDiff.onclick = () => {
        const open = detail.classList.toggle('hidden') === false;
        showDiff.setAttribute('aria-expanded', String(open));
        showDiff.textContent = open ? 'hide diff' : 'show diff';
        win.classList.toggle('pd-open', open);
        drawBrief();
    };
    deps.modal('Push commits', win);
    /** F27: "Then open a merge request into <target>", the title, the description behind a link, Generate, Draft */
    function mrSection(mr) {
        let base = null;
        const on = h('input', { type: 'checkbox', checked: true, 'aria-label': `Then open a ${mr.gh ? 'pull' : 'merge'} request` });
        const into = h('input', { type: 'text', class: 'mono pd-into', placeholder: 'target', 'aria-label': 'target branch' });
        const name = h('input', { type: 'text', class: 'pd-mrtitle', placeholder: 'title', 'aria-label': `${mr.gh ? 'PR' : 'MR'} title` });
        const desc = h('textarea', { rows: 6, class: 'mono pd-desc hidden', 'aria-label': `${mr.gh ? 'PR' : 'MR'} description` });
        const draft = h('input', { type: 'checkbox' });
        const descWhat = h('span', { class: 'mut' }, 'Description from commits');
        const genNote = h('span', { class: 'mut small gennote' });
        const edit = h('button', { class: 'link small', 'aria-expanded': 'false', onclick: () => {
                const open = desc.classList.toggle('hidden') === false;
                edit.setAttribute('aria-expanded', String(open));
                edit.textContent = open ? 'hide ‹' : 'edit ›';
                if (open)
                    desc.focus();
            } }, 'edit ›');
        const gen = h('button', { class: 'link small', title: 'draft the title and description from the branch\'s commits and diff (claude -p on this Mac, Haiku, no tools; files hidden from Claude are never sent)',
            onclick: () => void generate() }, ...generateLabel());
        const body = h('div', { class: 'pd-mrbody' }, name, h('div', { class: 'pd-mrrow small' }, descWhat, h('span', { class: 'mut' }, '·'), gen, h('span', { class: 'mut' }, '·'), edit, genNote, h('span', { class: 'grow' }), h('label', { class: 'small' }, draft, 'Draft')), desc);
        const el = h('div', { class: 'pd-mr' }, h('label', { class: 'pd-mron' }, on, h('span', {}, `Then open a ${mr.gh ? 'pull' : 'merge'} request into`), into), body);
        const sync = () => {
            body.classList.toggle('hidden', !on.checked);
            if (p) {
                goLabel.textContent = markBidi(pushGoLabel(p, on.checked, mr.gh));
                go.disabled = pushButton(p).disabled || (on.checked && (!name.value.trim() || !into.value.trim()));
                go.title = markBidi(on.checked && !go.disabled ? `push, then create the ${mr.gh ? 'PR' : 'MR'} in ${mr.gh ? 'GitHub' : 'GitLab'} - no second confirmation` : '');
            }
        };
        const fill = (d) => {
            if (!d)
                return;
            base = d;
            if (!into.value)
                into.value = d.target;
            name.value = d.title;
            desc.value = d.description;
            sync();
        };
        async function generate() {
            gen.disabled = true;
            gen.textContent = 'Generating…';
            try {
                const d = await mr.generate(into.value.trim());
                if (d) {
                    name.value = d.title || name.value;
                    desc.value = d.description;
                    descWhat.textContent = 'Description generated';
                    genNote.textContent = markBidi(generatedNote('the branch', d.withheld, d.truncated));
                }
            }
            catch (e) {
                genNote.textContent = markBidi('Generate failed: ' + errText(e));
            }
            finally {
                gen.disabled = false;
                gen.replaceChildren(...generateLabel());
                sync();
            }
        }
        on.addEventListener('change', sync);
        name.addEventListener('input', sync);
        into.addEventListener('change', () => { void mr.draft(into.value.trim()).then((d) => { if (d) {
            name.value = d.title;
            desc.value = d.description;
            sync();
        } }).catch(() => undefined); });
        into.addEventListener('input', sync);
        desc.addEventListener('input', () => { descWhat.textContent = 'Description edited'; });
        void mr.draft().then(fill).catch((e) => { genNote.textContent = markBidi('No draft: ' + errText(e)); });
        return {
            el, sync,
            values: () => on.checked && base ? { ...base, title: name.value.trim(), target: into.value.trim(), description: desc.value, draft: draft.checked } : null,
        };
    }
    function drawBrief() {
        clear(brief);
        if (!p)
            return;
        const now = Date.now() / 1000;
        const add = p.files.reduce((s, f) => s + f.add, 0);
        const del = p.files.reduce((s, f) => s + f.del, 0);
        if (!detail.classList.contains('hidden'))
            return void brief.append(h('div', { class: 'pd-filesline small' }, showDiff));
        brief.append(h('div', { class: 'pd-sub' }, 'Commits'));
        if (!p.commits.length)
            brief.append(h('p', { class: 'mut small pd-empty' }, 'No commits to push - a new branch.'));
        for (const c of p.commits) {
            brief.append(h('div', { class: 'pd-crow', title: `${c.hash}\n${c.author}${c.date ? ' · ' + relAge(Date.parse(c.date) / 1000, now) : ''}` }, h('code', { class: 'mut' }, c.short), h('span', { class: 'pd-csubj' }, c.subject), h('span', { class: 'mut small pd-cau' }, c.author)));
        }
        if (p.more)
            brief.append(h('p', { class: 'mut small pd-empty' }, '… and more'));
        if (p.files.length) {
            brief.append(h('div', { class: 'pd-filesline small' }, h('span', { class: 'link-ish' }, '› '), h('button', { class: 'link small', onclick: () => showDiff.click() }, `${p.files.length} ${p.files.length === 1 ? 'file' : 'files'} · ${stat(add, del)}`), h('span', { class: 'mut' }, '·'), showDiff));
        }
    }
    function destroyShown() {
        shown?.destroy();
        shown = null;
    }
    function draw() {
        if (!p)
            return;
        const x = p;
        const add = x.files.reduce((s, f) => s + f.add, 0);
        const del = x.files.reduce((s, f) => s + f.del, 0);
        clear(target);
        target.append(h('i', { class: 'fi fi-branch', 'aria-hidden': 'true' }), h('code', { class: 'pd-b' }, x.branch), h('span', { class: 'mut' }, '→'), h('code', { class: 'mut' }, x.remote), h('span', { class: 'mut' }, ':'), h('code', { class: 'pd-to' }, x.target), ...(x.new ? [h('span', { class: 'pd-new' }, 'new')] : []), h('span', { class: 'grow' }), h('span', { class: 'mut small' }, `${x.commits.length}${x.more ? '+' : ''} ${x.commits.length === 1 ? 'commit' : 'commits'} · ${x.files.length} ${x.files.length === 1 ? 'file' : 'files'} · ${stat(add, del)}`));
        clear(checks);
        for (const c of x.checks) {
            checks.append(h('div', { class: `pd-check ${c.level}` }, h('i', { class: `fi ${c.level === 'info' ? 'fi-info' : 'fi-alert'}`, 'aria-hidden': 'true' }), h('span', {}, c.text)));
        }
        cmd.textContent = markBidi(x.command);
        const b = pushButton(x);
        title.textContent = markBidi(b.label);
        goLabel.textContent = markBidi(b.label);
        go.disabled = b.disabled;
        mrBox?.sync();
        drawBrief();
        drawCommits();
    }
    function drawCommits() {
        if (!p)
            return;
        const now = Date.now() / 1000;
        clear(clist);
        if (!p.commits.length)
            return void clist.append(h('p', { class: 'mut small pd-empty' }, 'No commits to push.'));
        const row = (i, subject, c) => h('button', {
            class: 'gw-row' + (i === pick ? ' active' : ''), title: c ? `${c.hash}\n${c.author}` : '', onclick: () => void choose(i),
        }, h('span', { class: 'gw-subj' }, h('span', { class: 's' + (c ? '' : ' pd-all') }, subject)), c ? h('code', { class: 'gw-h link' }, c.short) : null, c ? h('span', { class: 'gw-au mut' }, c.author) : null, c ? h('span', { class: 'gw-age mut' }, c.date ? relAge(Date.parse(c.date) / 1000, now) : '') : null);
        clist.append(row(-1, `All ${p.commits.length}${p.more ? '+' : ''} ${p.commits.length === 1 ? 'commit' : 'commits'}`));
        p.commits.forEach((c, i) => clist.append(row(i, c.subject, c)));
    }
    async function choose(i) {
        if (!p)
            return;
        pick = i;
        drawCommits();
        const mine = ++cseq;
        if (i < 0)
            files = p.files;
        else {
            try {
                const c = (await api.gitCommitDetail(project, p.commits[i].hash)).data;
                if (mine !== cseq)
                    return;
                files = c?.files ?? [];
            }
            catch (e) {
                if (mine !== cseq)
                    return;
                files = [];
                clear(flist);
                flist.append(h('p', { class: 'bad small' }, errText(e)));
                return;
            }
        }
        drawFiles();
        const first = treeOrder(files, tree)[0];
        if (first)
            void showFile(first);
        else {
            file = null;
            clear(dhead);
            destroyShown();
            clear(dbox);
        }
    }
    function drawFiles() {
        clear(fhead);
        clear(flist);
        const add = files.reduce((s, f) => s + f.add, 0);
        const del = files.reduce((s, f) => s + f.del, 0);
        fhead.append(h('b', {}, `${files.length} ${files.length === 1 ? 'file' : 'files'}`), h('span', { class: 'gw-stat' }, stat(add, del)), h('span', { class: 'grow' }), h('button', { class: 'mini' + (tree ? ' on' : ''), 'aria-pressed': String(tree), onclick: () => { tree = true; setPref(TREE_KEY, '1'); drawFiles(); } }, 'Tree'), h('button', { class: 'mini' + (!tree ? ' on' : ''), 'aria-pressed': String(!tree), onclick: () => { tree = false; setPref(TREE_KEY, '0'); drawFiles(); } }, 'List'));
        const row = (f, full) => h('button', {
            class: 'gw-f' + (f === file ? ' active' : '') + (full ? '' : ' in'), title: f.old_path ? `${f.old_path} → ${f.path}` : f.path, onclick: () => void showFile(f),
        }, h('span', { class: `gw-st st-${f.status[0]}` }, f.status[0] ?? ''), h('span', { class: 'nm' }, full ? f.path : f.path.slice(f.path.lastIndexOf('/') + 1)), h('span', { class: 'grow' }), h('span', { class: 'gw-stat small' }, f.binary ? 'bin' : stat(f.add, f.del)), f.withheld ? hiddenMark() : null);
        if (tree) {
            for (const g of fileTree(files)) {
                if (g.dir)
                    flist.append(h('div', { class: 'gw-dir mut small', title: g.dir }, h('span', { class: 'fi fi-dir' }), shortDir(g.dir)));
                for (const f of g.files)
                    flist.append(row(f, false));
            }
        }
        else
            for (const f of files)
                flist.append(row(f, true));
    }
    function drawDiffHead() {
        clear(dhead);
        if (!file)
            return;
        const f = file;
        const order = treeOrder(files, tree);
        const i = order.indexOf(f);
        const seg = (v, label) => h('button', {
            class: 'mini' + (view === v ? ' on' : ''), 'aria-pressed': String(view === v),
            onclick: () => { if (view === v)
                return; view = v; setPref(VIEW_KEY, v); void showFile(f); },
        }, label);
        dhead.append(h('b', { class: 'nm', title: f.path }, f.path.slice(f.path.lastIndexOf('/') + 1)), h('span', { class: 'gw-stat small' }, stat(f.add, f.del)), h('span', { class: 'grow' }), h('span', { class: 'seg' }, seg('side', 'Side by side'), seg('unified', 'Unified')), h('button', { class: 'mini', title: 'previous file', 'aria-label': 'previous file', disabled: i <= 0, onclick: () => void showFile(order[i - 1]) }, '↑'), h('button', { class: 'mini', title: 'next file', 'aria-label': 'next file', disabled: i < 0 || i >= order.length - 1, onclick: () => void showFile(order[i + 1]) }, '↓'));
    }
    async function showFile(f) {
        if (!p)
            return;
        const x = p;
        const mine = ++fseq;
        file = f;
        drawFiles();
        drawDiffHead();
        destroyShown();
        clear(dbox);
        if (f.binary)
            return void dbox.append(h('p', { class: 'mut' }, 'Binary file - no text diff.'));
        dbox.append(h('p', { class: 'mut' }, 'loading…'));
        const st = f.status[0];
        const oldPath = f.old_path ?? f.path;
        // one commit: its parent and itself; all: the base and the newest commit
        const c = pick >= 0 ? x.commits[pick].hash : x.commits[0]?.hash ?? '';
        let left = '';
        let right = '';
        try {
            const [o, n] = await Promise.all([
                st === 'A' ? null : pick >= 0 ? api.gitBlob(project, c, oldPath, 'old') : x.base ? api.gitBlob(project, x.base, oldPath, 'new') : null,
                st === 'D' ? null : api.gitBlob(project, c, f.path, 'new'),
            ]);
            left = o?.data?.text ?? '';
            right = n?.data?.text ?? '';
        }
        catch (e) {
            if (mine !== fseq)
                return;
            clear(dbox);
            dbox.append(h('p', { class: 'bad' }, errText(e)));
            return;
        }
        if (mine !== fseq)
            return;
        clear(dbox);
        const d = await diffView(view, f, left, right);
        if (mine !== fseq)
            return void d.destroy();
        shown = d;
        dbox.append(d.dom);
    }
    const gone = new MutationObserver(() => {
        if (win.isConnected)
            return;
        gone.disconnect();
        destroyShown();
    });
    // #overlay: a layer over it or this layer going away (a replaced top layer leaves its parent untouched)
    const host = win.closest('#overlay') ?? win.parentElement?.parentElement;
    if (host)
        gone.observe(host, { childList: true });
    deps.preview().then((x) => {
        p = x;
        draw();
        void choose(-1);
    }).catch((e) => {
        clear(target);
        target.append(h('span', { class: 'bad' }, errText(e)));
    });
}
/** 9.112: the push dialog's Generate draws the same sparkles icon as the commit box, not a star glyph */
function generateLabel() {
    return [h('i', { class: 'fi fi-sparkles', 'aria-hidden': 'true' }), h('span', {}, 'Generate')];
}
//# sourceMappingURL=gitwindow.js.map