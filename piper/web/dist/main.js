// Piper GUI: activity bar · queue of what wants you · the detail of
// what you picked (a session: terminal / conversation / files / history + its context panel; the
// GitLab inbox; the production check) · the Global agent in a drawer (⌘0).
// State comes from GET /api/state (ETag poll, 1.5 s); terminals stream over /ws/<id>.
// Workbench views (9.6) share the main area with the terminals, which are only hidden
// (never re-parented).
import { isSend } from './sendkeys.js';
import './legacy.js'; // first: carries the pre-rename browser state over
import { absorbFragmentToken, api, ApiError, call, dropImage, forgetToken, fsIndex, downloadFile, projectGitStatus, sandboxFile as readSandboxFile, setToken, token } from './api.js';
import { createEditor } from './cmeditor.js';
import { createAudit, flowBar, flowStrip, istateButton, askedText } from './board.js';
import { appKey, appMod, hostKey, isMac, kbd } from './clipkeys.js';
import { createComposer } from './composer.js';
import { createConvo, hiddenPaths, resumePicker } from './convo.js';
import { ago, clear, fi, fold, h, unfold } from './dom.js';
import { createFiles } from './files.js';
import { createHttpPanel, httpTabWanted } from './httppanel.js';
import { httpApi } from './httpview.js';
import { classBadge, createDbView, dbApi, dbCard } from './dbview.js';
import { createDbSection } from './dbsection.js';
import { blockGroupKey, startupStatus, startupElapsed, attachLabel, deniedDomain, exitReason, issueTask, lastParagraph, plural, protectedText, showBidi, markBidi } from './text.js';
import { changeMark, createGitPanel, projectRel } from './gitpanel.js';
import { createInbox, inboxCommands, inboxName, sessionFlow } from './inbox.js';
import { createProd } from './prod.js';
import { cardDanger, createFeed, decidedElsewhere, feedApi, KIND_TEXT as PENDING_KIND } from './feed.js';
import { openMattermost, startPresence } from './mm.js';
import { createDiag } from './diag.js';
import { openHistory } from './history.js';
import { openMenu, placeMenu } from './menu.js';
import { parseToolPins, toggleToolPin, hiddenToolState, TOOL_PINS_KEY } from './navigation.js';
import { cardKey, cardModel, cardModule, decisionButtons, KIND_SHORT, moreLine, shownCard } from './decision.js';
import { NOTICE_MAX, NoticeLog } from './notices.js';
import { noticeWanted, showSecurityNotice } from './securitynotice.js';
import { restoreWanted, showRestoreNotice, wasWord } from './restorenotice.js';
import { LayerStack } from './layers.js';
import { branchNameProblem, shortDir, worktreeLabel, worktreeNameProblem } from './gitwindow.js';
import { createStack } from './stack.js';
import { createUsage } from './usage.js';
import { createLimits } from './limits.js';
import { createTimesheet, startFocusBeat } from './timesheet.js';
import { createSettings } from './settings.js';
import { createHostView, exitText, hostItems, splitHosts, tildePath } from './hostterm.js';
import { createSetup, keepOn, moduleOn, offModules } from './setup.js';
import { besideOnly, DB_HERE, dragSplit, FILES_PANE, fitFr, GRID_DB_MAX, GRID_MAX, gridPanes, hasPty, HTTP_PANE, keepPick, layoutChoices, layoutFor, migratePick, nextDbPane, paneKind, legacyFr, paneRects, presetOf, resetSplit, shapeKey, shapeOf, splitBars, splitBlock, swapPanes } from './gridlayout.js';
import { filterShortcuts, keyChips, keyGate, shownShortcuts } from './shortcuts.js';
import { editMembers, restoreMembers, gridShown, LAYOUT_KEY, layoutCommands, layoutReduce, LEGACY_KEYS, migrateLayout, persisted, queueShown as layQueueShown, stripShown, FOLD_VIEWS, QUEUE_VIEWS } from './layout.js';
import { openPalette } from './palette.js';
import { kindKey, optsSummary, parseForgeUrl, projectForRef, projectRow, projectsRoot, rebuildSummary, refChip, startOptionsDrafts, workKinds } from './startwork.js';
import { isoStatus, keepOpen, netStatus, sessionSummary, unlockRows } from './inspector.js';
import { ACTIVE, allowedFailed, canForget, canStop, continueLabel, createQueue, isTurn, monogram, moveTarget, projectShort, restoreToast, sessionName, sessionOrder } from './queue.js';
import { NavHistory } from './navhistory.js';
import { openUrl, TERM_FLAGS, TermView } from './term.js';
import { applyChoice } from './theme.js';
import { fileMatches, fileUrlReference, gitlabUrl, piperTarget, piperUrl, relativeFile } from './references.js';
const POLL_MS = 1500;
const BOARD_MS = 5000;
const LIVE = new Set(['ready', 'working', 'waiting', 'attention', 'trust', 'starting', 'attesting']);
const LABEL = {
    preflight: 'isolation check', blocked: 'blocked', preparing: 'preparing', spawning: 'spawning',
    trust: 'waiting for trust', starting: 'starting', attesting: 'verifying isolation', ready: 'ready',
    working: 'working', waiting: 'waiting for you', attention: 'needs attention', stopping: 'stopping',
    exited: 'ended', error: 'error',
};
let state = { sessions: [], pending: [], projects: [] };
let etag = '';
let active = '';
// W12: the one layout (layout.ts) - center Single / Grid (D1/D2: Beside and Split are grid panes), the left and the right; the booleans below
// are read from it (syncLayout), only dispatch() changes it. The context panel: on by default from 1400 px.
let lay = loadLayout();
let inspector = false; // the right column shows the context panel
let lastActive = ''; // what ⌘\ pairs with
let prevStates = new Map();
let prevPending = null; // confirmation ids already announced (null = not seeded yet)
// `${session id}:${detail}` of permission cards closed with × - N9: kept for the tab (sessionStorage), a reload brought
// every closed card back
const ASK_CLOSED_KEY = 'piper.askClosed';
const askClosed = new Set((() => { try {
    return JSON.parse(sessionStorage.getItem(ASK_CLOSED_KEY) ?? '[]');
}
catch {
    return [];
} })());
function closeAsk(key) {
    askClosed.add(key);
    try {
        sessionStorage.setItem(ASK_CLOSED_KEY, JSON.stringify([...askClosed].slice(-50)));
    }
    catch { /* private window: this page only */ }
}
let pendingSelect = ''; // a session just started from the GUI: switch to it once /api/state lists it
let view = 'term';
let grid = false; // ⌘G: up to 9 panes at once (sessions, host shells, Database - W26), in the layout the user picked (W3) - the grid (or a zoomed pane, 9.45) is the center
let gridAdd = false; // 9.38: the "Add to grid" cell shows (gridSlot)
let gridAddBack = null; // Cancel from Single restores its remembered grid
let gridPick = loadGridPick(); // the user's panes, in order (9.45: null only until the first state migrates the old Automatic)
let gridLayout = presetOf(readStore('piper.gridlayout') ?? 'auto').id;
try {
    window.localStorage.removeItem('piper.gridpins');
}
catch { /* ok */ } // 9.45: no pins
let gridFocus = ''; // W26: the grid pane with the keyboard ('' = the active session's)
let gridShape = { dir: 'col', groups: [] };
let gridFr = { main: [], cross: [] }; // splitter positions of gridShape, dragged in the gaps
/** views that are not about one session (no context panel, no composer) */
const GLOBAL_VIEWS = new Set(['gitlab', 'prod', 'audit', 'usage', 'timesheet', 'settings', 'db', 'setup', 'host']);
/** a view that belongs to a feature module: gone while the module is off */
// D8: Audit is core (the security log) - its icon and palette row in every preset, no module
const VIEW_MODULE = { http: ['http'], db: ['db'], gitlab: ['gitlab', 'github'], prod: ['prod'], usage: ['usage'], timesheet: ['timesheet'] };
/** a view whose module is off has none (W23: the inbox lives while GitLab or GitHub is on) */
function viewOn(v) {
    const vm = VIEW_MODULE[v];
    return !vm || vm.some((m) => on(m));
}
/** a feature module is on (an older piperd sends none: everything on) */
const on = (m) => moduleOn(state, m);
let drawer = false; // ⌘0: the chat with the Global agent (Feed on) - it has the right column
let queueStrip = true; // the strip, not the whole queue (the user's mode, or folded in Files / HTTP)
syncLayout();
// ⇧⌘F12 hid every panel: this pill says so and brings them back
const zenPill = h('button', { class: 'zenpill', hidden: !lay.zen, title: 'bring the panels back (⇧⌘F12)', onclick: () => dispatch({ t: 'zen' }) }, 'panels hidden · ', h('kbd', {}, '⇧⌘F12'));
let selKey = ''; // the queue item last opened (its detail is on screen)
let boardData = new Map(); // GET /api/board: last prompt / answer, flow, issue status
const terms = new Map();
const actbar = document.getElementById('actbar');
let toolPins = parseToolPins(readStore(TOOL_PINS_KEY));
let toolsButton = null;
let toolsMenu = null;
window.addEventListener('storage', (e) => {
    if (e.key !== TOOL_PINS_KEY && e.key !== null)
        return;
    toolPins = parseToolPins(readStore(TOOL_PINS_KEY));
    renderActbar();
});
/** W13: every toast and desktop notification, for the bell (D11: Recent alerts, kept across reloads) */
const notices = new NoticeLog({ get: () => window.localStorage.getItem('piper.alerts'), set: (v) => window.localStorage.setItem('piper.alerts', v) });
let closeNotices = null;
/** the bell's count follows a new notice (after the module is up: a toast can come before the queue exists) */
const bellSoon = () => { window.setTimeout(renderActbar, 0); };
const main = document.getElementById('main');
const insp = document.getElementById('inspector');
const overlay = document.getElementById('overlay');
function session(id) {
    return state.sessions.find((s) => s.id === id);
}
// ------------------------------------------------------------------ boot / token
function boot() {
    absorbFragmentToken();
    // `task piper:open` into a tab that is already open changes only the fragment: take the new token without a reload
    window.addEventListener('hashchange', () => {
        if (!absorbFragmentToken()) {
            if (polledOnce)
                sessionUrl(location.href);
            return;
        }
        if (document.getElementById('token-input'))
            closeModal();
        startBackground();
        if (!polling)
            void poll();
    });
    syncTheme();
    // N1: "Auto" follows the OS while the page is open
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => { if (themeNow === 'auto') {
        themeNow = '';
        syncTheme();
    } });
    window.matchMedia('(max-width: 640px)').addEventListener('change', () => { if (!bodyEl.classList.contains('grid'))
        files.setNarrow(phoneWidth()); });
    document.addEventListener('keydown', globalKeys);
    // browsers ignore a permission request outside a user gesture: ask on the first click
    if ('Notification' in window && Notification.permission === 'default') {
        document.addEventListener('pointerdown', () => void Notification.requestPermission(), { once: true });
    }
    else if ('Notification' in window && Notification.permission === 'denied' && firstTimeHere('piper.notification-help')) {
        // 9.107: once per browser - the bell's footer keeps saying it (it came back on every reload)
        toast('Desktop notifications are blocked for Piper - allow them in the browser\'s site settings (and macOS System Settings → Notifications)', undefined, 'notification-help');
    }
    if (!token()) {
        askToken();
        return;
    }
    startBackground();
    void poll();
}
/** the loops that call the API: only once there is a token (boot, or the token dialog) */
let backgroundStarted = false;
function startBackground() {
    if (backgroundStarted)
        return;
    backgroundStarted = true;
    startPresence();
    window.setInterval(() => {
        if (document.hidden)
            return;
        document.querySelectorAll('.startup-clock').forEach((el) => {
            el.textContent = markBidi(startupElapsed(Number(el.dataset.since)) + ' in this step');
        });
    }, 1000);
    startFocusBeat(() => (GLOBAL_VIEWS.has(view) ? '' : active), () => on('timesheet')); // 9.12 A: which session the user works in
    void loadBoard();
    // only while something shows it (the queue's lines, the header, the context panel), never in a hidden tab
    window.setInterval(() => { if (!document.hidden && QUEUE_VIEWS.has(view))
        void loadBoard(); }, BOARD_MS);
}
// the token dialog is up: closed any other way than Continue (Esc, a click outside), the GUI polls again -
// another tab may have stored a valid token meanwhile; a refused one brings the dialog back (never a silent frozen GUI)
let tokenAsked = false;
function askToken(msg = '') {
    polling = false;
    tokenAsked = true;
    const input = h('input', { id: 'token-input', type: 'password', class: 'mono', placeholder: 'token', autocomplete: 'off' });
    modal('Access to Piper', h('p', {}, (msg ? msg + ' ' : '') + 'Open the GUI again with `task piper:open` (the token comes in the URL), or paste the token from `gui-token` in Piper\'s home ($PIPER_HOME, ~/.piper by default).'), input, h('div', { class: 'row' }, h('button', {
        // N10-D-11: an empty field asks again in place (it polled with no token: 3× 401, "Invalid token")
        class: 'primary', onclick: () => {
            if (!input.value.trim())
                return input.focus();
            setToken(input.value);
            tokenAsked = false;
            closeModal();
            startBackground();
            void poll();
        },
    }, 'Continue')));
    // a pasted token + Enter continues (the one field of the dialog)
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value.trim()) {
        e.preventDefault();
        input.closest('.modal')?.querySelector('button.primary')?.click();
    } });
    input.focus();
}
// N9: another tab stored a token while this one asks for it: the dialog closes and the GUI polls with it
// (a refused one brings the dialog back) - before, it stayed up until the user pasted the token again
window.addEventListener('storage', (e) => {
    if (!tokenAsked || e.key !== 'piper.token' || !e.newValue)
        return;
    tokenAsked = false;
    closeModal();
    startBackground();
    void poll();
});
// ------------------------------------------------------------------ polling
let polledOnce = false;
let polling = false; // a poll loop is scheduled (the token dialog stops it, hashchange may restart it)
async function poll() {
    polling = true;
    try {
        const r = await api.state(etag);
        document.getElementById('banner')?.remove();
        if (r.data) {
            state = r.data;
            etag = r.etag;
            if (gridPick === null)
                storePick(migratePick(null, lay.center.kind === 'grid', liveIds())); // 9.45: Automatic is gone
            syncTheme();
            Object.assign(TERM_FLAGS, { hostLinks: !!state.gui_flags?.open_links_on_host, armEnter: !!state.gui_flags?.clipboard_arm_enter, dbSide: keyOn('⌘D'), clickFiles: !!state.gui_flags?.term_click_opens_files, nav: navOn() });
            notifyTransitions();
            inbox.sync(state.inbox);
            prod.sync(state.prod);
            feed.sync(state.feed);
            limits.sync(on('usage') ? state.limits : null, on('usage') && on('codex') ? state.codex_limits : null);
            restoreLast();
            syncFeatures();
            if (!on('composer') && composer.open())
                composer.toggle(false); // 9.44: the module went off (a restored draft stays stored)
            for (const x of state.sessions)
                if (x.kind === 'project')
                    files.sessionEdited(x.project, x.id, x.fsedit?.seq ?? 0, x.fsedit?.paths ?? [], x.fsedit?.unknown ?? 0);
            if (pendingSelect && session(pendingSelect)) {
                const id = pendingSelect;
                pendingSelect = '';
                select(id);
            }
            render();
            if (!polledOnce) { // a draft restored after a reload names its session once one is shown
                polledOnce = true;
                if (composer.open())
                    composer.sync();
                const target = piperTarget(new URL(location.href));
                if (target.session || target.view || target.path || target.table || target.commit)
                    sessionUrl(location.href);
            }
        }
    }
    catch (e) {
        if (e instanceof ApiError && e.status === 401) {
            document.getElementById('banner')?.remove(); // piperd answered: no "not responding" left over a restart
            // a refused link token falls back to the stored one; only a refused stored token asks
            if (!forgetToken(e.token)) {
                askToken('Invalid token.');
                return;
            }
            toast('The token in the link was refused - Piper goes on with the one saved in this browser.');
        }
        else {
            banner('piperd is not responding: ' + String(e instanceof Error ? e.message : e));
        }
    }
    window.setTimeout(() => void poll(), document.hidden ? POLL_MS * 4 : POLL_MS);
}
/** a file only the session's sandbox has (its /tmp scratchpad): read from the container, shown read-only */
async function sandboxFile(sid, path, line) {
    let text;
    try {
        text = await readSandboxFile(sid, path);
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
        return;
    }
    const box = h('div', { class: 'sbfile' });
    modal(path.slice(path.lastIndexOf('/') + 1), h('p', { class: 'mut small sbpath', title: path }, `${path} · in the sandbox only (not on this Mac), read-only`), box, h('div', { class: 'row' }, h('button', { onclick: () => void navigator.clipboard?.writeText(text).then(() => toast('Copied')) }, 'Copy'), button('Close', closeModal)));
    const ed = await createEditor({ path, text, readOnly: true, onDirty: () => { }, onCaret: () => { }, onSave: () => { },
        onDefinition: () => { }, onMenu: () => { }, onNote: () => { }, blame: () => null });
    box.append(ed.dom);
    if (line)
        ed.reveal(line, true);
}
/** policy nav_history: back / forward through sessions, views and opened files (mouse side buttons, ⌘[ / ⌘] outside Files) */
const nav = new NavHistory();
let navFile; // the file the user last opened in this session (Files tab or beside the terminal)
let navRestoring = false;
let navPop = false; // handling popstate: its own renders record nothing
let navIdx = 0; // this page's current browser history entry (history.state.piperNav)
let navSession = '';
function navOn() { return !!state.gui_flags?.nav_history; }
/** N1: the theme Settings › Interface chose (gui_flags.theme), applied when it changes; open terminals follow */
let themeNow = '';
function syncTheme() {
    const want = state?.gui_flags?.theme || window.__PIPER__?.theme || 'dark'; // before the first poll: the boot blob's
    if (want === themeNow)
        return;
    const first = !themeNow;
    themeNow = want;
    applyChoice(window.__PIPER__?.themes, want);
    if (!first)
        TermView.rethemeAll();
}
function navRecord() {
    if (!navOn() || navRestoring || navPop || !active)
        return;
    if (navSession !== active) {
        navSession = active;
        navFile = undefined;
    } // the pane follows the new session's project
    const file = view === 'files' || filesPaneShown() ? navFile : undefined;
    // every stop is also a browser history entry: Safari turns the mouse's side buttons (and a swipe) into
    // history back / forward without a DOM event - popstate below is the one way both arrive
    if (!navIdx)
        history.replaceState({ piperNav: navIdx = 1 }, '');
    if (nav.visit({ session: active, view, ...(file ? { file } : {}) }))
        history.pushState({ piperNav: ++navIdx }, '');
}
window.addEventListener('popstate', (e) => {
    const i = e.state?.piperNav;
    if (!navOn() || typeof i !== 'number' || i === navIdx)
        return;
    const dir = i < navIdx ? -1 : 1;
    navPop = true;
    try {
        for (let k = Math.abs(i - navIdx); k > 0; k--)
            navGo(dir); // a long-press history menu can jump several
    }
    finally {
        navPop = false;
    }
    navIdx = i;
});
function navGo(dir) {
    nav.prune((id) => !!session(id));
    const p = dir < 0 ? nav.goBack() : nav.goForward();
    if (!p)
        return;
    navRestoring = true;
    try {
        if (active !== p.session)
            activate(p.session);
        navSession = p.session;
        navFile = p.file;
        if (p.file && p.view === 'term') { // it was in the Files pane: the pane shows the files again
            showFilesPane();
        }
        setView(p.view);
        if (p.file) {
            files.show(p.file.project);
            void files.open(p.file.path, p.file.line);
        }
    }
    finally {
        navRestoring = false;
    }
    render();
}
// the mouse's side buttons (3 back, 4 forward): before the terminal and the browser's own history see them
window.addEventListener('mouseup', (e) => {
    if (!navOn() || (e.button !== 3 && e.button !== 4))
        return;
    e.preventDefault();
    e.stopPropagation();
    if (e.button === 3)
        history.back();
    else
        history.forward();
}, true);
/** policy restore_view: a reload comes back to the last session and view (onboarding still opens first) */
let lastRestored = false;
let lastSaved = '';
function restoreLast() {
    if (lastRestored)
        return;
    lastRestored = true;
    if (!state.gui_flags?.restore_view)
        return;
    try {
        const last = JSON.parse(readStore('piper.last') ?? 'null');
        if (last?.active && session(last.active))
            activate(last.active);
        if (last?.view)
            setView(last.view);
    }
    catch { /* a bad entry: the default start */ }
}
function saveLast() {
    if (!lastRestored || !state.gui_flags?.restore_view)
        return;
    const v = JSON.stringify({ view, active });
    if (v !== lastSaved) {
        lastSaved = v;
        writeStore('piper.last', v);
    }
}
/** after a poll: the first installation check, a hidden module's view left, the setup card redrawn */
let featSig = '';
let setupAuto = false; // not onboarded yet: the Setup view opens once per page load (a live session would hide the inline card)
let restoreShown = 0; // 9.141: the restore card's notice id, once per page (piperd forgets it as it opens)
let secNoticeShown = false; // the one-time Security screen: once per page (piperd forgets it as it opens)
function syncFeatures() {
    setup.prime();
    if (state.features && !state.features.onboarded && !setupAuto) {
        setupAuto = true;
        setView('setup');
    }
    if (!viewOn(view))
        view = 'term';
    reconcileLayout(); // a module / the Feed / the policy went off
    const sig = JSON.stringify([state.features ?? null, state.image_rebuild ?? {}]); // 9.54: the Setup image row follows a claimed / finished rebuild
    if (sig !== featSig) {
        featSig = sig;
        setup.redraw();
    }
    if (noticeWanted(state.security_notice, secNoticeShown, view === 'setup' || !!layers.top())) {
        secNoticeShown = true;
        showSecurityNotice(state.security_notice, { modal, closeModal, toast, openSecurity: () => { settings.show('security'); setView('settings'); } });
    }
    if (restoreWanted(state.interrupted_notice, restoreShown, view === 'setup' || !!layers.top())) {
        restoreShown = state.interrupted_notice.id;
        showRestoreNotice(state.interrupted_notice, restoreDeps());
    }
}
/** 9.141: the restore card's actions - Continue of each checked session, one after another */
function restoreDeps() {
    return {
        modal, closeModal, toast,
        running: () => state.sessions.filter((x) => ACTIVE.has(x.state)).length,
        restore: async (sid) => {
            try {
                await api.act(sid, 'retry');
                etag = '';
                return '';
            }
            catch (e) {
                return String(e instanceof Error ? e.message : e);
            }
        },
    };
}
/** the section's Restore…: the card again over the marked sessions (not marked seen again) */
function restoreInterrupted() {
    if (!state.interrupted_notice)
        return;
    restoreShown = state.interrupted_notice.id; // opened by hand: the poll does not open it once more after it closes
    showRestoreNotice(state.interrupted_notice, restoreDeps(), true);
}
function notifyTransitions() {
    const next = new Map(state.sessions.map((s) => [s.id, s.state]));
    for (const s of state.sessions) {
        const was = prevStates.get(s.id);
        if (was === 'working' && s.state !== 'working' && s.kind === 'project') { // Claude finished a turn
            git.invalidate(s.id);
            diag.invalidate(s.id);
        }
        if (was === 'working' && s.state !== 'working' && !onScreen(s.id))
            unseen.add(s.id);
        if (was && was !== s.state)
            boardSoon();
        if (was && was !== s.state && (s.state === 'waiting' || s.state === 'attention' || s.state === 'trust')
            && (!document.hasFocus() || s.id !== active)) {
            notify(`${s.title}: ${LABEL[s.state] ?? s.state}`, s.detail || s.project, s.id);
        }
        // F21: no toast for a session the Global agent started - the drawer lists it under "Started by the agent"
    }
    prevStates = next;
    // a confirmation card keeps its session `working` (the tool call waits), so no state change announces it
    const ids = new Set(state.pending.map((p) => p.id));
    if (prevPending) {
        for (const p of state.pending) {
            if (prevPending.has(p.id) || (!document.hidden && document.hasFocus()))
                continue;
            notify(`${p.title}: ${PENDING_KIND[p.kind] ?? p.kind}`, 'Waiting for your confirmation', `p:${p.id}`, () => (session(p.session) ? select(p.session, `a:p:${p.id}`) : undefined), p.session);
        }
    }
    prevPending = ids;
}
/** G3: one "needs you" number - the queue's Needs you (queue.needs()) - in the title, the app badge, the Overview
 *  badge and the strip; New (GitLab, production, CI) is a separate "new" count in the title only */
let badgeSig = '';
function renderBadges() {
    const needs = queue.needs().length;
    const fresh = queue.fresh().length;
    const sig = `${needs}|${fresh}`;
    if (sig === badgeSig)
        return;
    badgeSig = sig;
    const nav = navigator;
    if (needs && nav.setAppBadge)
        void nav.setAppBadge(needs).catch(() => undefined);
    else if (nav.clearAppBadge)
        void nav.clearAppBadge().catch(() => undefined);
    const words = [needs ? String(needs) : '', fresh ? `${fresh} new` : ''].filter(Boolean).join(' · ');
    document.title = markBidi(words ? `(${words}) Piper` : 'Piper');
}
let boardTimer = 0;
/** a state changed: the last answer / prompt of the queue and the detail follow within a second */
function boardSoon() {
    window.clearTimeout(boardTimer);
    boardTimer = window.setTimeout(() => void loadBoard(), 900);
}
/** a desktop notification; a click selects the session `id`, or runs `onClick` */
function notify(title, body, id, onClick, sessionId = onClick ? undefined : id) {
    // W13: in Recent alerts too - the desktop one vanishes and may be off
    notices.add(body ? `${title} · ${body.split('\n')[0]}` : title, 'desktop', { label: 'Open', run: () => { if (onClick)
            onClick();
        else
            select(id); } }, undefined, false, sessionId);
    bellSoon();
    if (!('Notification' in window))
        return;
    if (Notification.permission === 'default')
        void Notification.requestPermission();
    if (Notification.permission !== 'granted')
        return;
    const n = new Notification(markBidi(title), { body: markBidi(body), tag: id }); // the OS draws it: no h() on the way
    n.onclick = () => {
        window.focus();
        if (onClick)
            onClick();
        else
            select(id);
    };
}
// ------------------------------------------------------------------ render
function render() {
    syncLayout(); // 9.45: the grid shows from its panes - a session that ended may turn it into Single
    if (active && !session(active))
        active = '';
    if (!active) {
        const first = state.sessions.find((s) => LIVE.has(s.state) && s.kind === 'project' && !s.hidden)
            ?? state.sessions.find((s) => LIVE.has(s.state)) ?? state.sessions[0];
        if (first)
            active = first.id;
    }
    layout();
    queue.render();
    renderBadges();
    renderQueueMini();
    files.refreshWorking();
    renderActbar();
    renderMain();
    dismissViewedNotices();
    renderInspector();
    saveLast();
    navRecord();
    renderPending();
    if (drawer)
        drawDrawerState();
    for (const [id, t] of terms) {
        if (!session(id)) {
            t.dispose();
            terms.delete(id);
        }
    }
}
/** the queue is on screen: on, beside a view it belongs to - beside the grid too (W12 I6) */
function queueShown() {
    return layQueueShown(lay, view);
}
// F01 (reverts 9.57): the sessions are tiles in the rail under the pinned tools, whether the queue is open or folded.
// ⌘B / the Overview icon / the queue's fold button fold the whole queue to the rail alone (64 px). Files and HTTP fold
// it while there (W12: not stored - Terminal brings it back, ⌘B there keeps it whole). There a tile keeps the view.
const qmini = h('div', { id: 'qmini', role: 'group', 'aria-label': 'sessions' });
const qpeek = h('div', { class: 'qm-peek', role: 'tooltip' });
qpeek.hidden = true;
function stripOn() {
    return queueStrip;
}
function layout() {
    const mini = queueShown() && stripOn();
    document.body.classList.toggle('no-queue', !queueShown());
    document.body.classList.toggle('queue-mini', mini);
    qsplit.hidden = !queueShown() || mini;
}
/** ⌘B and the two fold buttons: the strip <-> the whole queue (a hidden queue comes back first) */
function toggleStrip() {
    dispatch({ t: 'strip' });
}
// a project's colour: the same project, the same tile, in every session
// ten hues far enough apart to tell at a glance; the Global agent (no project) is a neutral slate
const PROJECT_COLORS = ['#a78bfa', '#2dd4bf', '#f472b6', '#60a5fa', '#fb923c', '#a3e635', '#facc15', '#f87171', '#e879f9', '#d6a77a'];
function projectColor(p) {
    if (!p)
        return '#94a3b8';
    // the project's place in the configured list: neighbours (shop, shop2024) differ, the colour never changes
    const at = state.projects.findIndex((x) => x.key === p);
    let n = at;
    if (at < 0) {
        n = 2166136261; // not configured: FNV-1a of the name
        for (const c of p)
            n = Math.imul(n ^ c.charCodeAt(0), 16777619) >>> 0;
    }
    return PROJECT_COLORS[n % PROJECT_COLORS.length];
}
/** the tile's monogram (F01: two letters - acme-shop AS, shop2024 SH) and its #issue / project token (the peek's) */
function tileText(s) {
    if (s.kind === 'advisor')
        return ['GA', 'agent'];
    const name = s.project.split('/').pop() ?? s.project;
    const mono = monogram(name);
    if (s.issue)
        return [mono, `${s.issue.type === 'mr' ? '!' : '#'}${s.issue.iid}`];
    const nth = s.title.match(/ · (\d+)$/)?.[1];
    const token = name.match(/\d{2,}/)?.[0] ?? name.replace(/[^a-z0-9]/gi, '').slice(0, 4).toLowerCase();
    return [mono, nth ? `${token}·${nth}` : token];
}
function renderQueueMini() {
    // G4: the tiles in the one stable order of ⌘1–9 (sessionOrder: pins, your order); a failed / blocked start after
    // them. Waiting for you moves nothing: it is the tile's mark
    // the tile says what its Needs you row says (a blocked start: stuck, a question: wait)
    const waits = new Map(queue.needs().filter((x) => x.session).map((x) => [x.session, x.glyph]));
    const list = sessionOrder(state.sessions, railPins, railOrder);
    // after them: a failed / blocked start, and a session in any other state the queue's Needs you holds
    // (a crash = exited with a Needs you item) - it must not drop off the rail
    const onRail = new Set(list.map((s) => s.id));
    for (const s of state.sessions) {
        if (s.hidden || s.kind === 'prodcheck' || onRail.has(s.id))
            continue;
        if (s.state === 'error' || s.state === 'blocked' || waits.has(s.id)) {
            list.push(s);
            onRail.add(s.id);
        }
    }
    const sig = JSON.stringify([active, [...unseen], [...waits], list.map((s) => [s.id, s.state, s.title, s.project, s.issue?.iid, s.since, s.detail])]);
    if (qmini.dataset.sig === sig)
        return;
    qmini.dataset.sig = sig;
    clear(qmini);
    qpeek.hidden = true;
    qmini.hidden = !list.length;
    for (const s of list) {
        const [mono, num] = tileText(s);
        const tile = h('button', {
            class: 'qm' + (s.id === active ? ' on' : '') + (unseen.has(s.id) ? ' unseen' : ''), style: `--pc: ${projectColor(s.project)}`,
            'aria-label': `${s.title} · ${LABEL[s.state] ?? s.state}`, 'aria-current': s.id === active ? 'true' : undefined, onclick: () => openSession(s.id),
        }, h('span', { class: 'qm-mono' }, mono), h('span', { class: 'qm-num' }, num), h('span', { class: `st ${waits.get(s.id) ?? glyphOf(s.state)}`, 'aria-hidden': 'true' }), h('span', { class: 'qm-n', 'aria-hidden': 'true' }));
        tile.dataset.sid = s.id;
        tile.addEventListener('mouseenter', () => showPeek(s, tile));
        tile.addEventListener('mouseleave', () => { qpeek.hidden = true; });
        // N9: the same session menu as a queue row ("right click on a session" in Keyboard shortcuts); the ContextMenu key too
        tile.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            qpeek.hidden = true;
            openRailMenu(s, e.clientX || tile.getBoundingClientRect().right, e.clientY || tile.getBoundingClientRect().top, () => document.querySelector(`.qm[data-sid="${CSS.escape(s.id)}"]`)?.focus());
        }); // the compact rail's tile, not the hidden queue row
        tile.addEventListener('keydown', (e) => {
            if (e.key !== 'ContextMenu')
                return;
            e.preventDefault();
            const r = tile.getBoundingClientRect();
            openRailMenu(s, r.right, r.top);
        });
        qmini.append(tile);
    }
    numberStrip(); // more tiles than the height: the rail's middle scrolls (the active one is scrolled to)
    qmini.querySelector('button.qm.on')?.scrollIntoView({ block: 'nearest' });
}
/** ⌘1-9: the session's place in sessionOrder - the same number on the strip's tile and in the whole queue */
function sessionIds() {
    return sessionOrder(state.sessions, railPins, railOrder).map((s) => s.id);
}
function numberStrip() {
    const ids = sessionIds();
    for (const t of qmini.querySelectorAll('button.qm')) {
        const i = ids.indexOf(t.dataset.sid ?? '');
        t.querySelector('.qm-n').textContent = markBidi(i >= 0 && i < 9 ? String(i + 1) : '');
    }
}
/** hover over a tile: what the session is - the title, project · since, what it does, the keys */
function showPeek(s, tile) {
    const k = sessionIds().indexOf(s.id);
    const [, num] = tileText(s);
    clear(qpeek);
    qpeek.append(h('div', { class: 'qp-title' }, h('span', { class: `st ${glyphOf(s.state)}`, 'aria-hidden': 'true' }), h('span', {}, s.issue ? `${s.issue.type === 'mr' ? '!' : '#'}${s.issue.iid} ${s.issue.title}` : s.title)), h('div', { class: 'qp-sub' }, h('i', { style: `background: ${projectColor(s.project)}` }), `${s.project || num} · ${ago(s.since)}`), h('div', { class: 'qp-detail' }, sessionDetail(s) || (LABEL[s.state] ?? s.state)), h('div', { class: 'qp-keys' }, kbd([k >= 0 && k < 9 ? `⌘${k + 1} jump` : '', 'click open', '⌘B queue'].filter(Boolean).join(' · '))));
    placePeek(tile);
}
/** the peek next to a strip tile, inside the window */
function placePeek(tile) {
    const r = tile.getBoundingClientRect();
    qpeek.style.left = `${r.right + 10}px`;
    qpeek.style.top = `${Math.max(8, Math.min(r.top, window.innerHeight - 180))}px`;
    qpeek.hidden = false;
}
// the Claude limits (limits.ts): bars at the bottom of the whole queue; folded, the mini tiles by the bell (F01)
const limits = createLimits({
    open: () => toggleStrip(),
    peek: (nodes, anchor) => { clear(qpeek); qpeek.append(...nodes); placePeek(anchor); },
    hidePeek: () => { qpeek.hidden = true; },
});
/** a session from the queue, the strip or ⌘1-9: it becomes active and the tab stays where it can - Files / HTTP
 * follow its project, Conversation / History show its own; otherwise (and the Global agent in Files / HTTP) its Terminal */
function openSession(id, key = '') {
    const s = session(id);
    if (!s)
        return;
    qpeek.hidden = true;
    foldNarrowQueue(); // N10: a rail tile beside the whole queue (≤ 640 px) switched the session behind it
    const keep = (FOLD_VIEWS.has(view) ? s.kind === 'project' : view === 'convo' || view === 'history');
    if (!keep)
        return select(id, key);
    activate(id, key);
    if (view === 'files')
        files.show(s.project);
    else if (view === 'http')
        httpPanel.show(s.project);
    else if (view === 'convo') {
        convo.hide();
        convo.show(s.id);
    }
    else
        audit.show(s.id);
    render();
}
// the whole queue's width: a splitter on its right edge like the grid's (280-560 px, double-click = the default)
const Q_MIN = 280, Q_MAX = 560;
/** a narrow window keeps room for the detail: the chosen width, at most 40 % of the window */
const qWidth = (w) => `min(${w}px, 40vw)`;
const qsplit = h('div', { id: 'qsplit', title: 'drag to resize the queue, double-click = the default width' }, h('span', { class: 'qs-tip' }));
qsplit.hidden = true;
{
    const w = Number(readStore('piper.queue.w'));
    if (w >= Q_MIN && w <= Q_MAX)
        document.documentElement.style.setProperty('--q-w', qWidth(w));
}
qsplit.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    qsplit.setPointerCapture(e.pointerId);
    qsplit.classList.add('drag');
    for (const t of terms.values())
        t.freeze(true);
    const left = queue.el.getBoundingClientRect().left;
    const tip = qsplit.querySelector('.qs-tip');
    let w = 0;
    const move = (ev) => {
        const max = Math.max(Q_MIN, Math.min(Q_MAX, Math.floor(window.innerWidth * 0.4))); // what qWidth lets through
        w = Math.round(Math.min(max, Math.max(Q_MIN, ev.clientX - left)));
        document.documentElement.style.setProperty('--q-w', qWidth(w));
        tip.textContent = markBidi(`${w} px · ${Q_MIN} – ${max} · 2× click = default`);
    };
    const up = () => {
        qsplit.classList.remove('drag');
        qsplit.removeEventListener('pointermove', move);
        qsplit.removeEventListener('pointerup', up);
        qsplit.removeEventListener('pointercancel', up);
        if (w)
            writeStore('piper.queue.w', String(w));
        for (const t of terms.values())
            t.freeze(false);
    };
    qsplit.addEventListener('pointermove', move);
    qsplit.addEventListener('pointerup', up);
    qsplit.addEventListener('pointercancel', up);
});
qsplit.addEventListener('dblclick', () => {
    document.documentElement.style.removeProperty('--q-w');
    writeStore('piper.queue.w', '');
});
function dot(s) {
    return h('span', { class: 'dot st-' + (s.state === 'exited' && s.interrupted ? 'intr' : s.state), 'aria-hidden': 'true' });
}
/** a session state as the queue's mark (shape + colour; the word is next to it) */
function glyphOf(st) {
    return ['waiting', 'attention', 'trust'].includes(st) ? 'wait' : st === 'ready' ? 'idle' : ['error', 'blocked'].includes(st) ? 'stuck'
        : st === 'exited' || st === 'stopping' ? 'off' : 'run';
}
const NEEDS_YOU = ['attention', 'trust', 'waiting', 'blocked', 'error'];
// the order of Running, remembered in this browser (like the grid pick): dragged / ⌘⇧↑↓ order,
// pinned sessions (the keys keep their pre-9.16 names: nothing to migrate)
let railOrder = loadIds('piper.rail.order');
let railPins = new Set(loadIds('piper.rail.pins'));
/** sessions that finished a turn while you were not looking at them (cleared when you do) */
const unseen = new Set();
function loadIds(key) {
    try {
        const v = JSON.parse(readStore(key) ?? '[]');
        return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
    }
    catch {
        return [];
    }
}
function saveRailOrder(ids) {
    railOrder = ids;
    writeStore('piper.rail.order', JSON.stringify(ids));
    queue.render();
    renderQueueMini(); // N8: the strip's tiles and numbers too - ⌘2 opened another session than tile 2 until a poll changed something
}
function togglePin(s) {
    if (railPins.has(s.id))
        railPins.delete(s.id);
    else
        railPins.add(s.id);
    // forgotten sessions drop out of the stored set
    railPins = new Set([...railPins].filter((id) => session(id)));
    writeStore('piper.rail.pins', JSON.stringify([...railPins]));
    queue.render();
    renderQueueMini();
}
/** ⌘⇧↑ / ⌘⇧↓: move a session one place in Running (the same order as a drag) */
function moveInRail(id, d) {
    const ids = sessionIds();
    const i = ids.indexOf(id);
    const j = moveTarget(sessionOrder(state.sessions, railPins, railOrder), railPins, id, d);
    if (i < 0 || j < 0)
        return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    saveRailOrder(ids);
}
const ICONS = {
    overview: ['M3 3h18v18H3z', 'M3 9h18', 'M9 9v12'],
    grid: ['M4 4h7v7H4z', 'M13 4h7v7h-7z', 'M4 13h7v7H4z', 'M13 13h7v7h-7z'],
    agent: ['M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z', 'M9 10h.01', 'M12 10h.01', 'M15 10h.01'],
    usage: ['M4 20V11', 'M10 20V5', 'M16 20v-6', 'M3 20h18'],
    time: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z', 'M12 7v5l3 2'],
    audit: ['M12 3l8 3v6c0 5-3.5 8-8 9c-4.5-1-8-4-8-9V6z', 'M9 12l2 2l4-4'],
    stacks: ['M12 3l9 5l-9 5l-9-5z', 'M3 13l9 5l9-5'],
    db: ['M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3s-3.6 3-8 3s-8-1.3-8-3z', 'M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6', 'M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3'],
    settings: ['M4 7h9', 'M17 7h3', 'M4 17h3', 'M11 17h9', 'M15 5v4', 'M9 15v4'],
    'panel-open': ['M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z', 'M9 3v18', 'M14 9l3 3l-3 3'],
    'panel-close': ['M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z', 'M9 3v18', 'M16 15l-3-3l3-3'],
    bell: ['M6 16V11a6 6 0 0 1 12 0v5l2 2H4z', 'M10 20a2 2 0 0 0 4 0'],
    inbox: ['M4 4h16l2 10v5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-5z', 'M2 14h6l2 3h4l2-3h6'],
    prod: ['M3 12h4l3-8l4 16l3-8h4'],
    keys: ['M3 6h18v12H3z', 'M7 10h.01', 'M11 10h.01', 'M15 10h.01', 'M8 14h8'],
    host: ['M4 5l7 7l-7 7', 'M13 19h7'],
    more: ['M5 12h.01', 'M12 12h.01', 'M19 12h.01'],
    pin: ['M16 9l3 3v2H5v-2l3-3V3h8z', 'M12 14v7'],
};
function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    for (const d of ICONS[name] ?? []) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
/** The same available destinations feed pinned buttons and All tools. Disabled modules keep their stored pins. */
function navigationTools() {
    const dash = window.__PIPER__?.dashboardUrl;
    const tools = [
        on('prod') && state.prod?.on && { id: 'prod', label: 'Production', key: '', active: view === 'prod', title: 'the morning production check: errors and alerts', run: () => setView('prod'), count: state.prod.new ?? 0 },
        on('db') && { id: 'db', label: 'Database', key: '', active: view === 'db', title: 'databases of your projects: tables, SQL console, connections, Claude\'s queries', run: () => openDb() },
        state.host_terms?.on && { id: 'host', label: 'Host shells', key: '⌥F12', active: view === 'host', title: 'login shells on this Mac, no sandbox', run: () => toggleHost(), danger: true },
        on('usage') && { id: 'usage', label: 'Usage', key: '', active: view === 'usage', title: 'tokens per day and project; plan limits under the sessions', run: () => setView('usage') },
        on('timesheet') && { id: 'time', label: 'Time', key: '', active: view === 'timesheet', title: 'timesheet: your time by issue and day', run: () => setView('timesheet') },
        { id: 'audit', label: 'Audit', key: '', active: view === 'audit', title: 'security and operations records of piperd', run: () => setView('audit') },
        !!dash && on('stack') && state.stack_up !== false && { id: 'stacks', label: 'Dev stacks', key: '', active: false, title: 'Dev stacks dashboard ↗', href: dash, run: () => { window.open(dash, 'dev-stacks', 'noopener'); } },
        { id: 'keys', label: 'Keys', key: '?', active: false, title: 'keyboard shortcuts', run: () => shortcuts() },
    ];
    return tools.filter((t) => !!t);
}
function toolEntries() {
    return [{ group: 'All tools' }, ...navigationTools().map((t) => ({
            id: t.id, label: h('span', { class: 'nav-tool-label' }, icon(t.id), h('span', {}, t.label), t.count ? h('span', { class: 'nav-tool-count' }, String(t.count)) : null, t.href ? h('span', { 'aria-hidden': 'true' }, '↗') : null),
            search: t.label, title: t.title, keys: t.key ? kbd(t.key) : undefined, on: t.active,
            danger: t.danger && t.active, run: t.run,
            secondary: { label: `Pin ${t.label}`, activeLabel: `Unpin ${t.label}`, on: toolPins.includes(t.id), icon: icon('pin'), run: (pinned) => {
                    toolPins = toggleToolPin(toolPins, t.id, pinned);
                    writeStore(TOOL_PINS_KEY, JSON.stringify(toolPins));
                    renderActbar();
                } },
        }))];
}
function toggleTools() {
    if (toolsMenu) {
        toolsMenu.close(true);
        return;
    }
    if (!toolsButton)
        return;
    toolsMenu = openMenu(toolsButton, toolEntries(), { label: 'All tools', side: 'right', className: 'nav-tools', search: true,
        foot: 'Pin your tools to the sidebar · ← → choose action',
        back: () => toolsButton?.focus({ preventScroll: true }),
        closed: () => {
            toolsMenu = null;
            actSig = '';
            // An outside mousedown still needs to click its original rail button on mouseup.
            toolsButton?.classList.remove('expanded');
            toolsButton?.setAttribute('aria-expanded', 'false');
        },
    });
    renderActbar();
}
let actSig = '';
function renderActbar() {
    // F01: the rail's number is Blocked only (red: something stands); Your turn is the tiles' dots and the queue
    const needs = queue.needs().filter((x) => !isTurn(x)).length;
    const adv = state.sessions.find((s) => s.kind === 'advisor' && (LIVE.has(s.state) || s.state === 'blocked' || s.state === 'preflight'));
    const chat = state.feed?.on ? (state.feed.chat_queued ?? 0) : 0;
    const dash = window.__PIPER__?.dashboardUrl;
    const onAgent = drawer || (!state.feed?.on && view === 'term' && session(active)?.kind === 'advisor');
    const overviewOn = queueShown() && !stripOn() && view === 'term' && !onAgent && !zoomed();
    const tools = navigationTools();
    const hidden = hiddenToolState(tools, toolPins);
    const sig = JSON.stringify([needs, !!adv, adv?.state, chat, view, grid, overviewOn, onAgent, dash, state.inbox?.new, state.prod?.new,
        state.features?.modules, state.stack_up, notices.unread, !!closeNotices, !!state.host_terms?.on, !!state.prod?.on, toolPins, !!toolsMenu, queueShown() && !stripOn()]);
    if (sig === actSig)
        return;
    actSig = sig;
    // icons only: the name is in the tooltip and the accessible label
    const item = (name, label, key, on, title, run, badge) => h('button', { class: 'ab-item' + (on ? ' on' : ''), title: `${label} - ${title}${key ? ` (${kbd(key)})` : ''}`, 'aria-label': label, 'aria-pressed': String(on), onclick: run }, icon(name), badge ?? null);
    const badge = (n, cls, title) => (n ? h('span', { class: 'ab-badge ' + cls, title }, n > 99 ? '99+' : String(n)) : null);
    const focusedLabel = actbar.contains(document.activeElement) ? document.activeElement?.getAttribute('aria-label') : null;
    const scrollTop = actbar.querySelector('.ab-main')?.scrollTop ?? 0;
    if (!toolsButton)
        toolsButton = h('button', { class: 'ab-item', 'aria-label': 'All tools', 'aria-haspopup': 'menu', onclick: toggleTools }, icon('more'));
    toolsButton.className = 'ab-item' + (hidden.active ? ' on' : '') + (hidden.danger ? ' host' : '') + (toolsMenu ? ' expanded' : '');
    toolsButton.setAttribute('aria-pressed', String(!!hidden.active));
    toolsButton.setAttribute('aria-expanded', String(!!toolsMenu));
    toolsButton.title = showBidi('All tools - open or pin tools' + (hidden.active ? ` · ${tools.find((t) => t.id === hidden.active)?.label} active` : ''));
    toolsButton.replaceChildren(icon('more'));
    const hiddenBadge = badge(hidden.count, 'warn', `${hidden.count} new in hidden tools`);
    if (hiddenBadge)
        toolsButton.append(hiddenBadge);
    clear(actbar);
    const pinned = tools.filter((t) => toolPins.includes(t.id)).map((t) => {
        if (t.href)
            return h('a', { class: 'ab-item', 'aria-label': t.label, href: t.href, target: 'dev-stacks', rel: 'noopener', title: t.title }, icon(t.id));
        const button = item(t.id, t.label, t.key, t.active, t.title, t.run, badge(t.count ?? 0, '', `${t.count ?? 0} new in ${t.label}`));
        return t.danger ? hostItem(button) : button;
    });
    const els = [
        h('div', { class: 'ab-brand', title: 'Piper' }, h('img', { src: '/piper/icon.svg', alt: 'Piper' })),
        // D12: lit only while it is the active place - the whole queue beside the terminal, no other icon's view
        item('overview', 'Overview', '', overviewOn, overviewOn ? 'fold the queue to the rail (⌘B)' : 'the whole queue: what is blocked, your turn, what is running (⌘B fold, ⇧⌘F12 hide)', () => dispatch({ t: 'overview', on: !overviewOn }), badge(needs, 'bad', `${needs} blocked - ${plural(needs, 'an agent waits', 'agents wait', 'agents wait')} for your decision`)),
        !on('agent') ? null : item('agent', 'Agent', '⌘0', onAgent, state.feed?.on ? 'chat with the Global agent' : adv ? 'the Global agent\'s terminal' : 'start the Global agent', () => openAgent(), chat ? badge(chat, '', `${chat} queued for the agent`) : adv ? null : h('span', { class: 'ab-dot off', title: 'not running' })),
        // W17: the shared forge inbox and the production report are places with their own icon (IntelliJ's stripe buttons) -
        // the queue's GitLab / Production pills only filter the queue
        !viewOn('gitlab') ? null : item('inbox', inboxName(state.inbox), '', view === 'gitlab', `the ${inboxName(state.inbox) === 'Inbox' ? 'GitLab + GitHub' : inboxName(state.inbox)} inbox: assigned, mentions, ${on('gitlab') ? 'MRs' : 'PRs'}, with preview and actions`, () => setView('gitlab'), badge(state.inbox?.new ?? 0, '', `${state.inbox?.new ?? 0} new in the inbox`)),
        pinned.length ? h('div', { class: 'ab-sep', role: 'separator' }) : null,
        ...pinned,
        // F01: the sessions under the pinned tools (the same element across renders: renderQueueMini fills it)
        h('div', { class: 'ab-sep ab-sess-sep', role: 'separator' }),
        qmini,
        h('div', { class: 'grow' }),
        toolsButton,
        // F01: the plan limits by the bell while the queue (and its bars) is folded
        queueShown() && !stripOn() ? null : limits.tile,
        item('settings', 'Settings', '⌘,', view === 'settings', 'features, GitLab, projects, sessions, isolation', () => setView('settings')),
        item('bell', 'Recent alerts', '', !!closeNotices, 'the last 24 hours, up to 100 alerts - kept across reloads in this browser', () => { if (closeNotices)
            closeNotices(false);
        else
            openNotices(); }, badge(notices.unread, 'warn', `${notices.unread} unread`))
    ];
    const split = els.findIndex((el) => el?.classList.contains('grow'));
    const present = (xs) => xs.filter((x) => !!x);
    actbar.append(els[0], h('div', { class: 'ab-main' }, ...present(els.slice(1, split))), h('div', { class: 'ab-foot' }, ...present(els.slice(split + 1))));
    actbar.querySelector('.ab-main').scrollTop = scrollTop;
    toolsMenu?.update(toolEntries());
    if (focusedLabel && !toolsMenu) {
        const replacement = [...actbar.querySelectorAll('[aria-label]')].find((el) => el.getAttribute('aria-label') === focusedLabel);
        (replacement ?? toolsButton).focus({ preventScroll: true });
    }
}
function hostItem(b) {
    b.classList.add('host');
    return b;
}
/** ⌥F12: the host Terminal view (W2) and back to the session's terminal */
function toggleHost() {
    setView(view === 'host' ? 'term' : 'host');
}
/** a host shell in `project` (session ⋯ menu, palette): the view, then the shell */
function openHostShell(project) {
    setView('host');
    if (view === 'host')
        void hostView.open(project);
}
/** the Overview icon: the queue on / off (off = the whole width for the terminal); on focuses it */
function toggleQueue(on) {
    dispatch(on === undefined ? { t: 'queue' } : { t: 'queue', on }); // W12 I6: beside the grid too - it no longer turns the grid off
}
/** ⌘⇧L: the keyboard into the queue from anywhere (the terminal keeps Esc) */
function focusQueue() {
    if (!queueShown())
        return toggleQueue(true);
    if (stripOn())
        return toggleStrip(); // the strip has no list to focus: the whole queue first
    queue.focus();
}
/** the keyboard back to what the detail shows (the terminal, a view) */
function focusDetail() {
    if (view === 'term') {
        const s = session(active);
        if (s && hasPty(s))
            terms.get(active)?.focus();
        else {
            const panel = grid ? sessionPanels.get(active) : panelEl;
            if (panel) {
                if (!grid)
                    panel.tabIndex = -1;
                panel.focus({ preventScroll: true });
            }
        }
    }
    else
        (main.querySelector('.view input, .view button, .view [tabindex="0"]'))?.focus({ preventScroll: true });
}
/** ≤ 640 px the whole queue takes the detail's place (app.css): a pick from it folds to the strip, the detail shows -
 *  not stored, the user's whole-queue mode stays (⌘B / Overview bring it back) */
function foldNarrowQueue() {
    if (matchMedia('(max-width: 640px)').matches && queueShown() && !stripOn())
        dispatch({ t: 'narrow' });
}
/** the queue's item opens its detail */
function openItem(it) {
    foldNarrowQueue();
    selKey = it.key; // GitLab / Production keep it; a session sets it in select()
    const a = it.action;
    const e = it.event;
    const subj = a?.subject ?? e?.subject;
    if (a && (a.level === 'gitlab' || a.subject.type === 'issue' || a.subject.type === 'mr'))
        return openGitlab(a.subject.key);
    if (a?.level === 'prod' || subj?.type === 'prod')
        return setView('prod');
    if (e && (subj?.type === 'issue' || subj?.type === 'mr'))
        return openGitlab(subj.key);
    if (it.session && session(it.session)) {
        openSession(it.session, it.key);
        return;
    }
    const url = a?.actions.find((o) => o.op === 'url')?.args.url;
    if (typeof url === 'string' && /^https?:\/\//.test(url))
        window.open(url, '_blank', 'noopener');
    else
        toast('Nothing to open for this item');
}
let glKey = ''; // the inbox item the GitLab view was opened on
function openGitlab(key) {
    glKey = key;
    if (view !== 'gitlab')
        setView('gitlab');
    if (!inbox.reveal(key))
        toast('The item is no longer in the GitLab inbox');
}
function isSelected(it) {
    if (view === 'gitlab')
        return !!it.action && it.action.subject.key === glKey && (it.source === 'gitlab');
    if (view === 'prod')
        return it.source === 'prod';
    if (!QUEUE_VIEWS.has(view) || !it.session || it.session !== active)
        return false;
    // the item it was opened from; when that one is gone (answered), every item of the session
    return !selKey || selKey === it.key || !queue.has(selKey);
}
/** ⌘0 / the Agent button: the chat drawer (Feed on), else the agent's terminal (started if needed) */
function openAgent() {
    if (!on('agent'))
        return;
    if (state.feed?.on)
        return toggleDrawer();
    const adv = state.sessions.find((s) => s.kind === 'advisor' && (LIVE.has(s.state) || s.state === 'blocked' || s.state === 'preflight'));
    if (adv) {
        select(adv.id);
    }
    else
        void start('advisor', '', '');
}
// ---- context menu of a session (right click in the queue, or the context-menu key on a focused item)
/** a session's actions (queue row, strip tile, board, the ⋯ in its own header) - F28: one version
 *  everywhere, what does not apply now is disabled with the reason; an icon column, keys only where they exist;
 *  Continue / Stop… and Remove last, red, under a line */
function openRailMenu(s, x, y, back) {
    const live = LIVE.has(s.state) || s.state === 'stopping';
    const project = s.kind === 'project';
    const inGrid = gridMembers().includes(s.id);
    const order = sessionOrder(state.sessions, railPins, railOrder);
    const inList = order.some((x) => x.id === s.id);
    const besideWhy = splitBlock(session(active), s);
    const open = (v) => () => { select(s.id); if (v !== 'term')
        setView(v); };
    const ended = s.state === 'exited' || s.state === 'error' || s.state === 'blocked';
    const switchTo = switchTarget(s, on('codex'));
    const entries = [
        { label: 'Open', icon: 'fi-open', keys: '↵', run: open('term'), disabled: s.id === active && !grid && view === 'term' && 'this session is open' },
        on('grid') ? { label: 'Side by side', icon: 'fi-columns', run: () => splitWith(s.id), disabled: besideWhy || false, keys: kbd('⌘\\'),
            title: 'into the grid right after the active session (9.45: nothing in the grid is replaced)' } : false,
        hasTerminal(s) && on('grid') ? { label: inGrid ? 'Remove from grid' : 'Add to grid', icon: inGrid ? 'fi-square-minus' : 'fi-square-plus', run: () => dispatch(inGrid ? { t: 'remove', id: s.id } : { t: 'add', id: s.id }) } : false,
        { group: 'View' },
        { label: 'Files', icon: 'fi-dir', run: open('files'), disabled: !project && 'only a project session has files' },
        on('http') ? { label: 'HTTP requests', icon: 'fi-http', run: open('http'), disabled: !project && 'only a project session has HTTP requests' } : false,
        { label: 'Conversation', icon: 'fi-chat', run: open('convo') },
        { label: 'History', icon: 'fi-history', run: open('history') },
        live && s.pid !== null && !s.tty ? { label: attachLabel(state.terminal_app), icon: 'fi-attach', run: () => void openInTerminal(s),
            title: 'piper attach in a new terminal window (Settings > Terminal): that terminal sets the size, Ctrl-] detaches, the session keeps running' } : false,
        project && state.host_terms?.on ? { label: 'Host shell here', icon: 'fi-term', run: () => openHostShell(s.project),
            title: 'a login shell on this Mac in the project\'s folder - no sandbox' } : false,
        { group: 'Order' },
        s.kind !== 'advisor' ? { label: railPins.has(s.id) ? 'Unpin' : 'Pin to top', icon: 'fi-pin', run: () => togglePin(s),
            title: 'a pinned session stays on top, even after it ends' } : false,
        inList ? { label: 'Move up', icon: 'fi-up', run: () => moveInRail(s.id, -1), keys: kbd('⌘⇧↑'),
            disabled: moveTarget(order, railPins, s.id, -1) < 0 && 'already at the top (pinned sessions stay on top)' } : false,
        inList ? { label: 'Move down', icon: 'fi-down', run: () => moveInRail(s.id, 1), keys: kbd('⌘⇧↓'),
            disabled: moveTarget(order, railPins, s.id, 1) < 0 && 'already at the bottom (pinned sessions stay on top)' } : false,
        s.kind !== 'advisor' ? { label: 'Rename…', icon: 'fi-pen', run: () => renameSession(s) } : false,
        null,
        switchTo ? { label: `Continue with ${agentName(switchTo)}…`, icon: 'fi-fwd', run: () => confirmSwitch(s),
            disabled: !s.claude_session && 'no saved conversation to hand over yet',
            title: `stops this session and hands its conversation to ${agentName(switchTo)} as a file (9.140)` } : false,
        ended ? { label: continueLabel(s), icon: 'fi-refresh', run: () => void act(s, 'retry'), ...(rebuildArmed() ? { title: 'rebuilds the sandbox image first (several minutes)' } : {}) } : false,
        canStop(s) ? { label: 'Stop…', icon: 'fi-square', run: () => confirmStop(s), danger: true } : false,
        canForget(s) ? { label: 'Remove', icon: 'fi-x', run: () => void act(s, 'forget'), title: 'from the list - transcript and audit stay', danger: true } : false,
    ];
    popMenu(entries, x, y, `actions ${s.kind === 'advisor' ? 'Global agent' : s.title}`, back ?? (() => document.querySelector(`.queue [data-sid="${CSS.escape(s.id)}"]`)?.focus()));
}
/** a click on path:line in `v`'s terminal: Piper's editor - the Files tab (full) or the pane beside; IntelliJ for the
 *  Global agent, which has no Files view */
function openTermFile(v, path, line, full) {
    if (v.kind !== 'project') {
        openInIdea(path.startsWith('/') ? path : `${v.path}/${path}`, line);
        return;
    }
    if (active !== v.id)
        referenceSession(v.id);
    if (full) {
        setView('files');
    }
    else
        showFilesPane(); // D1: the grid's Files pane, no view switch (9.7 #1)
    files.show(v.project);
    files.open(path, line);
}
/** A reference to a session outside the current grid must actually reveal its terminal. */
function referenceSession(id) {
    select(id);
    if (view === 'term' && grid && !gridIdsShown().includes(id))
        dispatch({ t: 'single' });
}
/** All destinations keep the emitting session's project, even when another grid cell is active. */
function linkTask(task) { void task.catch(e => toast(e instanceof Error ? e.message : String(e))); }
function linkChoices(title, choices) {
    openMenu({ x: Math.max(8, Math.min(window.innerWidth - 320, 360)), y: 120 }, choices, { label: title, search: true, placeholder: title });
}
async function openReference(v, ref, full = false) {
    if (ref.kind === 'session') {
        const matches = state.sessions.filter(s => s.id === ref.id || s.id.startsWith(ref.id));
        if (matches.length === 1)
            referenceSession(matches[0].id);
        else if (matches.length)
            linkChoices('Choose session', matches.map(s => ({ label: s.title, run: () => referenceSession(s.id) })));
        else
            toast('Session not found');
        return;
    }
    if (ref.kind === 'gitlab') {
        const projects = ref.project ? [ref.project] : [...new Set(v.gitlab ?? [])];
        if (!projects.length || !state.gitlab_host) {
            toast('No GitLab project is configured for this session');
            return;
        }
        const go = (project) => openUrl(gitlabUrl(state.gitlab_host, project, ref.resource, ref.id), toast, sessionUrl);
        if (projects.length === 1)
            go(projects[0]);
        else
            linkChoices('Choose GitLab project', projects.map(project => ({ label: project, run: () => go(project) })));
        return;
    }
    if (v.kind !== 'project') {
        if (ref.kind === 'file' && ref.path.startsWith('/tmp/'))
            await sandboxFile(v.id, ref.path, ref.line);
        else
            toast('This reference needs a project session');
        return;
    }
    if (ref.kind === 'commit') {
        // Verify the hash in this repository before showing the existing history / diff UI.
        await api.gitCommitDetail(v.project, ref.hash);
        referenceSession(v.id);
        openHistory({ toast, modal, closeModal, openFile: openBeside }, v.project, '', { commit: ref.hash });
        return;
    }
    if (ref.kind === 'db') {
        await openDbReference(v, ref.name);
        return;
    }
    const rel = relativeFile(ref.path, v.path);
    if (rel === null) {
        if (ref.path.startsWith('/tmp/'))
            await sandboxFile(v.id, ref.path, ref.line);
        else
            toast('This path is outside the session project');
        return;
    }
    const show = async (path, directory = false) => {
        referenceSession(v.id);
        if (directory) {
            showFilesPane();
            await files.openDirectory(v.project, path);
        }
        else if (/\.(?:http|rest)$/i.test(path) && on('http')) {
            setView('http');
            await httpPanel.openFile(v.project, path, ref.line);
        }
        else if (/\.(?:pdf|zip|gz|xlsx?|docx?|pptx?)$/i.test(path)) {
            await downloadFile(v.project, path);
            toast(`Downloaded ${path.split('/').pop()}`);
        }
        else {
            openTermFile(v, path, ref.line, full);
            if (ref.diagnostic && on('diag'))
                files.showDiagnostics();
        }
    };
    if (rel === '.') {
        await show(rel, true);
        return;
    }
    const slash = rel.lastIndexOf('/'), parent = slash < 0 ? '.' : rel.slice(0, slash), name = rel.slice(slash + 1);
    try {
        const listing = (await api.list(v.project, parent)).data;
        const entry = listing?.entries.find(e => e.name.normalize('NFC') === name.normalize('NFC'));
        if (entry) {
            await show(parent === '.' ? entry.name : `${parent}/${entry.name}`, entry.type === 'dir');
            return;
        }
    }
    catch (e) {
        if (!(e instanceof ApiError) || e.status !== 404)
            throw e;
    }
    const matches = fileMatches((await fsIndex(v.project, true)).data?.files ?? [], rel);
    if (matches.length === 1)
        await show(matches[0]);
    else if (matches.length)
        linkChoices('Choose file', matches.map(path => ({ label: path, run: () => linkTask(show(path)) })));
    else
        toast(`File not found in this project: ${rel}`);
}
async function openDbReference(v, name, connection = '') {
    if (!on('db')) {
        toast('Enable Databases in Settings → Features');
        return;
    }
    if (!/^[\p{L}_][\p{L}\p{N}_$]*(?:\.[\p{L}_][\p{L}\p{N}_$]*)?$/u.test(name))
        throw new Error('Invalid table reference');
    const connections = ((await dbApi.connections(v.project, v.id)).data?.connections ?? []).filter(c => c.project === v.project);
    const chosen = connection ? connections.find(c => c.name === connection) : connections.length === 1 ? connections[0] : undefined;
    if (!chosen) {
        if (!connections.length) {
            toast('No database connection for this project');
            return;
        }
        if (connection)
            throw new Error('The database connection does not belong to this project');
        linkChoices('Choose database connection', connections.map(c => ({ label: `${c.label || c.name} · ${c.class}`, run: () => linkTask(openDbReference(v, name, c.name)) })));
        return;
    }
    const parts = name.split('.'), table = parts.pop(), specified = parts[0];
    const schemas = specified ? [specified] : (await dbApi.meta(v.project, chosen.name, 'schemas', '', '', v.id)).data?.schemas ?? [];
    if (schemas.length > 32)
        throw new Error('Use schema.table to choose among more than 32 schemas');
    const hits = [];
    // Metadata only; neither terminal text nor a pasted SQL statement is executed.
    for (const schema of schemas) {
        const objects = (await dbApi.meta(v.project, chosen.name, 'objects', schema, '', v.id)).data?.objects ?? [];
        if (objects.some(o => o.name === table))
            hits.push(schema);
    }
    const show = async (schema) => {
        referenceSession(v.id);
        openDbPane(chosen.name);
        const pane = paneShown(DB_HERE) ? dbPane(DB_HERE).view : dbView;
        if (pane === dbView)
            openDb(chosen.name);
        await pane.openReference(v.project, chosen.name, schema, table);
    };
    if (hits.length === 1)
        await show(hits[0]);
    else if (hits.length)
        linkChoices('Choose table schema', hits.map(schema => ({ label: `${schema}.${table}`, run: () => linkTask(show(schema)) })));
    else
        toast(`Table not found: ${name}`);
}
/** Return true for every Piper URL, including unknown routes: never hand them to Chrome. */
function sessionUrl(uri, source = session(active)) {
    if (/^file:\/\//i.test(uri)) {
        try {
            const ref = fileUrlReference(uri);
            if (source)
                linkTask(openReference(source, ref));
        }
        catch {
            toast('Invalid file link');
        }
        return true;
    }
    const url = piperUrl(uri, location.href, window.__PIPER__?.origins);
    if (!url)
        return false;
    try {
        const target = piperTarget(url);
        const owner = target.session ? session(target.session) : source;
        if (target.session && !owner) {
            toast('Session not found');
            return true;
        }
        if (owner)
            referenceSession(owner.id);
        if (target.path && owner)
            linkTask(openReference(owner, { kind: 'file', path: target.path, line: target.line }, target.view === 'files'));
        else if (target.table && owner)
            linkTask(openDbReference(owner, target.table, target.connection));
        else if (target.commit && owner)
            linkTask(openReference(owner, { kind: 'commit', hash: target.commit }));
        else if (['term', 'files', 'http', 'db', 'convo', 'history', 'gitlab', 'prod', 'audit', 'usage', 'timesheet', 'settings', 'setup', 'host'].includes(target.view))
            setView(target.view);
        else if (url.pathname === '/' || target.session) {
            if (owner)
                setView('term');
        }
        else
            toast('This Piper link has no supported destination');
    }
    catch {
        toast('Invalid Piper link');
    }
    return true;
}
// Markdown / GitLab anchors use the same browser policy as terminal URLs. Modified clicks must
// not turn an internal Piper link into a new Chrome window either.
function anchorLink(e) {
    const anchor = e.target?.closest?.('a[href]');
    if (!anchor || anchor.hasAttribute('download') || !/^https?:/i.test(anchor.href))
        return;
    if (piperUrl(anchor.href, location.href, window.__PIPER__?.origins)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        sessionUrl(anchor.href);
    }
    else if (TERM_FLAGS.hostLinks) {
        e.preventDefault();
        e.stopImmediatePropagation();
        openUrl(anchor.href, toast, sessionUrl);
    }
}
document.addEventListener('click', anchorLink, true);
document.addEventListener('auxclick', e => { if (e.button === 1)
    anchorLink(e); }, true);
function openInIdea(abs, line) {
    window.location.href = `idea://open?file=${encodeURIComponent(abs)}&line=${line || 1}`;
}
/** F04: a plain click on path:line asks where - Open in Files ↵, Open beside ⇧↵, Open in IDEA,
 *  Copy path; ⌘/Ctrl- and ⇧-click keep their direct ways (Files tab / IntelliJ) */
function pathMenu(e, v, rel, abs, line) {
    const at = line ? `:${line}` : '';
    const proj = v.kind === 'project' && !!rel;
    const sandbox = !rel && abs.startsWith('/tmp/');
    const copy = () => void navigator.clipboard?.writeText((rel || abs) + at).then(() => toast(`Copied ${(rel || abs) + at}`), () => toast('Copy refused by the browser'));
    const back = () => terms.get(v.id)?.focus();
    openMenu({ x: e.clientX, y: e.clientY + 6 }, [
        proj ? { label: 'Open in Files', keys: '↵', run: () => openTermFile(v, rel, line, true) } : false,
        proj ? { label: 'Open beside', keys: '⇧↵', run: () => openTermFile(v, rel, line, false) } : false,
        sandbox ? { label: 'Open (sandbox file)', run: () => void sandboxFile(v.id, abs, line), title: 'read-only, from the session\'s sandbox' } : false,
        { label: 'Open in IDEA', run: () => openInIdea(abs, line), ...(proj || sandbox ? {} : { title: 'outside the project - only if it is on this Mac' }) },
        { label: 'Copy path', run: copy },
    ], { label: `${rel || abs}${at}`, className: 'pathmenu', back, search: false, ...(proj ? { shiftEnter: () => openTermFile(v, rel, line, false) } : {}) });
}
/** a context menu at x/y (W11: the one menu component); `back` gets the focus when it closes */
function popMenu(entries, x, y, label, back) {
    openMenu({ x, y }, entries, { label, ...(back ? { back } : {}) });
}
/** the session's terminal is shown right now (the active one, a grid pane) */
function onScreen(id) {
    if (drawer && session(id)?.kind === 'advisor')
        return true; // 9.14: its answers are in the chat drawer
    if (view !== 'term')
        return false;
    return id === active || (grid && gridIdsShown().includes(id));
}
/** Only a session actually visible in the foreground acknowledges its alerts. */
function dismissViewedNotices() {
    if (document.hidden || !document.hasFocus())
        return;
    const ids = state.sessions.filter((s) => (drawer && s.kind === 'advisor') ||
        (view === 'convo' && s.id === active) ||
        (view === 'term' && (grid ? gridIdsShown().includes(s.id) : s.id === active))).map((s) => s.id);
    if (notices.dismissSessions(ids))
        bellSoon();
}
window.addEventListener('focus', dismissViewedNotices);
document.addEventListener('visibilitychange', dismissViewedNotices);
/** `key`: the queue item it was opened from (its highlight, its confirmation card) */
/** the session becomes the active one (the view stays) */
function activate(id, key = '') {
    unseen.delete(id);
    gridFocus = ''; // the grid marks the active session's pane again
    if (id !== active && active)
        lastActive = active;
    applyLayout({ t: 'activate', id }); // I4: in the grid it stays; a grid of one session + Files / DB follows it (D1); elsewhere alone
    selKey = key;
    active = id;
    if (composer.open())
        composer.sync();
}
function select(id, key = '') {
    activate(id, key);
    // attached in a native terminal, which sets the size - the readable view first (Terminal is a tab away)
    setView(!grid && session(id)?.tty ? 'convo' : 'term');
    terms.get(id)?.focus();
}
/** `piper attach <id>` in a new terminal window (Settings > Terminal); elsewhere the command goes to the clipboard */
async function openInTerminal(s) {
    try {
        const r = (await api.openTerminal(s.id)).data;
        if (!r)
            return;
        if (r.ok) {
            toast(`Opened in ${r.terminal ?? 'the terminal'} - Ctrl-] there detaches, ${s.kind === 'advisor' ? 'the Global agent' : 'the session'} keeps running`);
            return;
        }
        const copied = await navigator.clipboard?.writeText(r.command).then(() => true, () => false);
        toast(`${r.error ?? 'Could not open a terminal'} - ${copied ? 'copied' : 'run'}: ${r.command}`);
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
    }
}
/** N10-D-12: into the terminal view for a terminal layout (Composer, grid, ⌘\\, ⌘O / ⌘D); false = it stayed where it
 *  was (unsaved Settings and the user chose Stay) - the caller switches nothing on (the Composer opened over Settings) */
function toTerm(then) {
    if (view !== 'term')
        setView('term', then); // G-N11-8: "Discard" goes on with what was asked (it landed in the bare terminal)
    return view === 'term';
}
/** then: what to do once the view is v, when unsaved Settings asked first and the user chose Discard (G-N11-8) */
function setView(v, then) {
    if (v === view) {
        render();
        return;
    }
    if (view === 'settings' && settings.dirty()) { // unsaved Settings: ask before leaving
        modal('Unsaved settings', h('p', {}, 'Settings has unsaved changes. Discard them?'), h('div', { class: 'row' }, button('Stay', closeModal), h('button', { class: 'danger', onclick: () => {
                closeModal();
                settings.discard();
                setView(v);
                if (view === v)
                    then?.();
            } }, 'Discard')));
        return;
    }
    if (view === 'files' && v !== 'files')
        files.flush(); // 9.41: leaving Files saves a pending edit now
    if (view === 'convo')
        convo.hide();
    if (view === 'host')
        hostView.hide();
    composerDismissed = false;
    if (!viewOn(v))
        return; // a module that is off has no view (a key, an old link)
    // into Files / HTTP: the whole queue folds to the strip while there (the code gets the width) - not stored (W12 #5)
    applyLayout({ t: 'view', to: v });
    view = v;
    const s = session(active);
    if (v === 'files')
        files.show(s && s.kind === 'project' ? s.project : files.project());
    else if (v === 'http')
        httpPanel.show(s && s.kind === 'project' ? s.project : '');
    else if (v === 'db')
        dbView.show();
    else if (v === 'convo' && s)
        convo.show(s.id);
    else if (v === 'history' && s)
        audit.show(s.id);
    else if (v === 'audit')
        audit.show('');
    else if (v === 'gitlab')
        inbox.redraw();
    else if (v === 'prod')
        prod.redraw();
    else if (v === 'usage')
        usage.show();
    else if (v === 'timesheet')
        timesheet.show();
    else if (v === 'settings')
        settings.show();
    else if (v === 'setup')
        setup.open();
    else if (v === 'host')
        hostView.show();
    // D7: the Composer lives under the terminal and Conversation; the one Conversation opened goes with it
    if ((v !== 'term' && v !== 'convo') || (composerAuto && v !== 'convo'))
        composer.toggle(false);
    composerAuto = false;
    if (v === 'files' && s && s.kind === 'project')
        git.prefetch(s.id);
    render();
}
// The body (and the xterm elements in it) is created once and never re-parented: a poll
// only swaps the header and the state panel, so a live terminal is not moved or hidden.
const bodyEl = h('div', { class: 'body' });
const viewEl = h('div', { class: 'view hidden' });
let headEl = h('header', { class: 'head' });
// D1: the grid's Files pane lends the Files view's browser (one instance - the Files tab takes it back, they are never
// on screen together)
const filesLabel = h('div', { class: 'tlabel' });
const filesPaneEl = h('div', { class: 'term filespane hidden' }, filesLabel);
// 9.42: the grid's HTTP pane lends the HTTP tab's panel the same way
const httpLabel = h('div', { class: 'tlabel' });
const httpPaneEl = h('div', { class: 'term httppane hidden' }, httpLabel);
let httpFor = ''; // the session the HTTP pane followed last
let filesFor = ''; // the session the Files pane followed last (once per switch: a poll must not redraw the tree)
let dbHereFor = ''; // the same for the Database pane of the active session's project
let panelEl = null;
const sessionPanels = new Map();
bodyEl.append(viewEl);
function renderMain() {
    const s = session(active);
    if (!s && !GLOBAL_VIEWS.has(view))
        view = 'term';
    // the Database view is its own full-height workspace (its header line is in its left column)
    const nextHead = view === 'db' || view === 'host' ? h('header', { class: 'head hidden' }) : GLOBAL_VIEWS.has(view) ? globalHeader() : s ? header(s) : h('header', { class: 'head hidden' });
    // the same header stays (a poll changed something else): no flicker, the spinner keeps turning
    nextHead.dataset.sid = s?.id ?? '';
    if (nextHead.outerHTML !== headEl.outerHTML) {
        headEl.replaceWith(nextHead);
        headEl = nextHead;
    }
    bodyEl.classList.toggle('flush', view === 'db');
    if (view !== 'term') {
        for (const t of terms.values())
            t.el.classList.add('hidden');
        for (const el of sessionPanels.values())
            el.classList.add('hidden');
        hideGridExtras([]);
        // G-N11-6: the grid's splitters (z-index 3) and Add cell stayed over the view (Database's Add, Settings' sections)
        bodyEl.classList.remove('grid');
        clearGridLayout();
        slotEl.remove();
        files.setNarrow(phoneWidth()); // 9.109: the Files tab at phone width is the narrow pane (tree as an overlay)
        panelEl?.remove();
        panelEl = null;
        const want = view === 'files' ? files.el : view === 'http' ? httpPanel.el : view === 'db' ? dbView.el : view === 'convo' ? convo.el : view === 'gitlab' ? gitlabView : view === 'prod' ? prodView
            : view === 'usage' ? usage.el : view === 'timesheet' ? timesheet.el : view === 'settings' ? settings.el : view === 'setup' ? setup.el : view === 'host' ? hostView.el : audit.el;
        if (viewEl.firstChild !== want) {
            clear(viewEl);
            viewEl.append(want);
        }
        viewEl.classList.remove('hidden');
        if (view === 'files')
            files.resumeHttp();
        else if (view === 'http')
            httpPanel.resume();
        convoComposer(s);
        if (view === 'gitlab') {
            inbox.redraw();
        }
        else if (view === 'host') {
            hostView.sync();
        }
        else if (view === 'prod') {
            prod.redraw();
            prodOff.classList.toggle('hidden', !!state.prod?.on);
        }
        return;
    }
    viewEl.classList.add('hidden');
    // a session without a terminal (blocked, ended) shows its panel alone
    const pty = (x) => !!x && hasPty(x);
    const visible = (grid ? gridSessions() : [s]).filter(pty);
    const shownIds = grid ? gridIdsShown() : [];
    // the focused pane: a host shell / Database pane the keyboard is in, else the active session
    const focusId = shownIds.includes(gridFocus) && paneKind(gridFocus) !== 'session' ? gridFocus : active;
    for (const v of visible)
        if (!terms.has(v.id)) {
            const t = new TermView(v.id, v.path, {
                onCopyFailed: (message, retry) => toast(message, retry),
                onRefused: (why, retry) => toast(why === 'locked' ? 'The terminal is locked until the isolation check has passed.' : `Input refused: ${why}`, retry),
                onExit: () => undefined,
                onConnection: () => undefined,
                onImage: (f) => dropImage(v.id, f),
                onOutside: (path, line) => (path.startsWith('/tmp/') ? void sandboxFile(v.id, path, line)
                    : toast(`${path} is outside the project - ⇧+click opens it in IntelliJ if it is on this Mac.`)),
                onUrl: (uri) => sessionUrl(uri, v),
                onReference: (ref, e) => linkTask(openReference(v, ref, e.metaKey || e.ctrlKey)),
                onOpenFile: (path, line, full) => openTermFile(v, path, line, full),
                onPathMenu: (e, rel, abs, line) => pathMenu(e, v, rel, abs, line),
                onFocus: () => { if (grid && active !== v.id)
                    makeActive(v.id); },
                onConversation: () => {
                    if (active !== v.id)
                        select(v.id);
                    setView('convo');
                },
            });
            terms.set(v.id, t);
            t.el.dataset.sid = v.id;
            t.mount(bodyEl);
        }
    bodyEl.classList.toggle('grid', grid);
    files.setNarrow(grid && shownIds.includes(FILES_PANE));
    for (const t of terms.values()) {
        const show = visible.some((v) => v.id === t.id);
        const was = !t.el.classList.contains('hidden');
        t.el.classList.toggle('hidden', !show);
        t.el.classList.toggle('focused', grid && t.id === focusId);
        if (grid && show) {
            paneLabel(t.label, t.id);
            wireGridFocus(t.el, t.id);
        }
        t.el.querySelector(':scope > .progress')?.remove();
        if (grid && show) {
            const v = session(t.id);
            if (v && ['preflight', 'preparing', 'spawning', 'starting', 'attesting'].includes(v.state)) {
                const progress = statePanel(v);
                if (progress)
                    t.el.append(progress);
            }
        }
        if (show && !was)
            t.refit();
    }
    // Starting / blocked / ended-without-PTY sessions still occupy their own pane beside Add or a project tool.
    for (const el of sessionPanels.values())
        el.classList.add('hidden');
    if (grid)
        for (const v of gridSessions().filter((x) => !hasPty(x))) {
            let el = sessionPanels.get(v.id);
            if (!el) {
                el = h('div', { class: 'term sessionpane', 'data-sid': v.id, tabindex: '0' }, h('div', { class: 'tlabel' }), h('div', { class: 'sessionstatus' }));
                el.addEventListener('focusin', () => { if (grid && active !== v.id)
                    makeActive(v.id); });
                sessionPanels.set(v.id, el);
            }
            el.classList.remove('hidden');
            el.classList.toggle('focused', v.id === focusId);
            if (el.parentElement !== bodyEl)
                bodyEl.append(el);
            paneLabel(el.firstElementChild, v.id);
            const content = el.lastElementChild;
            const next = statePanel(v) ?? h('p', { class: 'mut' }, 'No terminal yet');
            if (content.firstElementChild?.outerHTML !== next.outerHTML) {
                clear(content);
                content.append(next);
            }
            wireGridFocus(el, v.id);
        }
    // W26: the grid's host-shell, Database and (D1) Files panes (a host shell's terminal is the Host view's, Files the
    // Files view's browser - both lent to the grid)
    for (const id of shownIds) {
        const k = paneKind(id);
        if (k === 'session')
            continue;
        const t = k === 'host' ? hostView.term(id) : null;
        const d = k === 'db' ? dbPane(id) : null;
        const el = t?.el ?? d?.el ?? (k === 'http' ? httpPane() : filesPane());
        const was = el.parentElement === bodyEl && !el.classList.contains('hidden');
        if (el.parentElement !== bodyEl)
            bodyEl.append(el);
        if (t)
            t.mount(bodyEl);
        el.classList.remove('hidden');
        if (k === 'files')
            files.resumeHttp();
        else if (k === 'http')
            httpPanel.resume();
        el.classList.toggle('hostpane', k === 'host');
        el.classList.toggle('focused', id === focusId);
        paneLabel(t?.label ?? d?.label ?? (k === 'http' ? httpLabel : filesLabel), id);
        wireGridFocus(el, id);
        if (t && !was)
            t.refit();
        // the active session's Database pane follows its project once per switch
        if (d && (!was || (id === DB_HERE && dbHereFor !== active)))
            d.view.show();
        if (id === DB_HERE)
            dbHereFor = active;
    }
    hideGridExtras(shownIds);
    // bodyEl keeps the order of first mount; the grid orders its panes by CSS `order`
    if (grid)
        shownIds.forEach((id, i) => { const el = gridEl(id); if (el)
            el.style.order = String(i); });
    else
        visible.forEach((v, i) => {
            const t = terms.get(v.id);
            if (!t)
                return;
            t.el.style.order = String(i);
        });
    // Polls rebuild Single status panels; retain keyboard ownership only if the old panel had it.
    const panelFocused = !!panelEl?.contains(document.activeElement);
    panelEl?.remove();
    slotEl.remove();
    if (grid) {
        hostView.sync(); // shells closed elsewhere: their terminals go
        const slot = gridSlot(shownIds.length);
        if (slot)
            bodyEl.append(slot);
        gridCells = [...shownIds.map((id) => gridEl(id)).filter((x) => !!x), ...(slot ? [slot] : [])];
        layoutGrid();
        panelEl = gridCells.length ? null : h('div', { class: 'empty' }, h('h1', {}, 'The grid is empty'), h('p', {}, kbd('Nothing to show: no live session, host shell or Database pane. ⌘G turns the grid off.')));
        if (panelEl)
            bodyEl.append(panelEl);
        if (panelFocused)
            focusDetail();
        return;
    }
    clearGridLayout();
    // no session: the setup card while it is wanted (a new install, a check that fails), else the hub
    panelEl = s ? statePanel(s) : setup.wanted(state) ? h('div', { class: 'panel setup-panel' }, setup.el) : emptyHub();
    // the end bar is a strip above the body: inside it (a row beside the file pane) it would be a column
    if (panelEl?.classList.contains('endbar'))
        bodyEl.before(panelEl);
    else if (panelEl)
        bodyEl.append(panelEl);
    if (panelFocused)
        focusDetail();
}
// D7: under Conversation the Composer opens collapsed by itself for a live session (the reply bar it replaced);
// Esc closes it until the next visit, leaving the view takes an auto-opened one along
let composerAuto = false;
let composerDismissed = false;
let hiddenTabs = loadHiddenTabs(); // 9.44: tabs / toggles the user unticked (right-click the session bar)
/** the session takes text now (piperd types it while Claude works too: Claude Code queues it, G5) */
function sendable(s) {
    return !!s && s.pid !== null && s.kind !== 'prodcheck' && ['waiting', 'attention', 'ready', 'working'].includes(s.state);
}
function convoComposer(s) {
    if (view !== 'convo' || !on('composer'))
        return;
    if (sendable(s)) {
        if (!composer.open() && !composerDismissed) {
            composer.toggle(true, { compact: true, focus: false });
            composerAuto = true;
        }
    }
    else if (composerAuto && composer.open()) {
        composer.toggle(false); // the session ended / is not one: nothing to reply to
    }
}
/** Claude Code's own English status lines ("Claude is waiting for your input") say nothing the state does not */
function sessionDetail(s) {
    if (!s.detail || /waiting for your input|needs your permission|^claude /i.test(s.detail))
        return '';
    return s.state === 'attention' || s.state === 'error' || s.state === 'stopping' || s.state === 'exited' ? s.detail : '';
}
// F09 (9.66): the HTTP tab only for a project with a .http file (or while it is the view) - asked per project,
// again after a minute (a file made from the empty state shows the tab on the next look)
const httpFilesOf = new Map();
function httpTab(project) {
    const known = httpFilesOf.get(project);
    if (!known || Date.now() - known.at > 60000) {
        httpFilesOf.set(project, { has: known?.has ?? true, at: Date.now() }); // until the answer: as it was
        void httpApi.files(project).then(({ data }) => {
            if (!data)
                return;
            const has = data.files.length > 0;
            if (httpFilesOf.get(project)?.has !== has) {
                httpFilesOf.set(project, { has, at: Date.now() });
                render();
            }
        }).catch(() => undefined);
    }
    return httpTabWanted(httpFilesOf.get(project).has, on('http'), view === 'http');
}
function header(s) {
    const live = LIVE.has(s.state) && s.pid !== null;
    const ended = s.state === 'exited' || s.state === 'error' || s.state === 'blocked';
    const card = boardData.get(s.id);
    const issue = card?.issue ?? s.issue ?? null;
    const tab = (v, label, title) => h('button', { class: 'tab' + (view === v ? ' on' : ''), title, 'aria-pressed': String(view === v), onclick: () => setView(v) }, label);
    // D13: the Global agent is its own kind - no project, so no Files / HTTP (not even disabled).
    // F03 (9.69): what goes beside the terminal is one "Beside ▾" (besideMenu); right-click = which tabs the bar shows
    const proj = s.kind === 'project';
    const shown = (k) => !hiddenTabs.has(k);
    const tabs = h('nav', { class: 'tabs', 'aria-label': 'session view', oncontextmenu: (e) => tabsMenu(e, s) }, tab('term', 'Terminal', 'the session terminal'), shown('convo') ? tab('convo', 'Conversation', 'readable conversation and token usage') : null, proj && shown('files') ? tab('files', 'Files', kbd('the whole project - files hidden from Claude marked with the eye (⌘P file, ⌘⇧F search)')) : null, proj && httpTab(s.project) && shown('http') ? tab('http', 'HTTP', 'every .http file of the project: run requests on dev / test / prod, results') : null, shown('history') ? tab('history', 'History', 'what happened in this session: prompts, cards, tool calls, exits') : null);
    const actions = h('div', { class: 'actions' });
    // F03: Stop is an icon (a square, its word in the tooltip); G1: a start (preflight…spawning) too
    if (canStop(s))
        actions.append(h('button', { class: 'icon dh-stop', title: 'Stop the session', 'aria-label': 'Stop', onclick: () => confirmStop(s) }, h('i', { class: 'fi fi-square', 'aria-hidden': 'true' })));
    if (ended && (grid || s.state !== 'exited')) { // Single has the exit bar; the grid needs header actions
        // N8: a blocked start in Single has its own primary in the body (Approve and start) - one green button (T1)
        actions.append(h('button', { class: s.state === 'blocked' && !grid && approvesInBody(s) ? '' : 'primary', onclick: () => void act(s, 'retry') }, continueLabel(s)));
        if (canForget(s))
            actions.append(h('button', { title: 'from the list (transcript and audit stay)', onclick: () => void act(s, 'forget') }, 'Remove'));
    }
    actions.append(h('button', { class: 'icon', title: 'more: attach, grid, host shell, order, rename', 'aria-label': 'more actions', onclick: (e) => {
            const r = e.currentTarget.getBoundingClientRect();
            // the bar redraws on every poll: the focus goes back to the ⋯ that is there when the menu closes
            openRailMenu(s, r.left - 180, r.bottom + 4, () => document.querySelector('button[aria-label="more actions"]')?.focus());
        } }, h('i', { class: 'fi fi-more', 'aria-hidden': 'true' })));
    const att = state.gui_flags?.attestation === false || s.attest?.skipped ? null : s.attest;
    const iso = att ? h('button', { class: 'chip ' + (att.ok ? 'ok' : 'bad'), title: att.ok ? `isolation verified (${att.checks} checks) - details in the context panel` : 'isolation failed - details in the context panel', onclick: () => revealIsolation() }, att.ok ? '✓ isolated' : '✗ isolation') : null;
    const detail = sessionDetail(s);
    const word = s.state === 'waiting' ? 'waiting for you' : LABEL[s.state] ?? s.state;
    // F03: one 36 px bar - state, the name (the project's last segment when the title is the project; the path in its
    // tooltip, no project chip), branch / isolation / issue quietly, the tabs, Beside ▾, the context panel, ■, ⋯
    const name = s.kind === 'advisor' ? 'Global agent' : headTitle(s);
    return h('header', { class: 'head detail one', 'data-state': s.state }, h('div', { class: 'dh-top' }, h('span', { class: `st ${glyphOf(s.state)}`, role: 'img', 'aria-label': word,
        title: `${word} since ${new Date((s.since || s.created) * 1000).toLocaleString()}` }), h('div', { class: 'title', title: s.kind === 'advisor' ? 'all projects' : `${s.project} · ${s.path} · double-click = rename`,
        ondblclick: () => { if (s.kind !== 'advisor')
            renameSession(s); } }, name), h('div', { class: 'dh-chips' }, s.agent === 'codex' ? h('span', { class: 'chip', title: 'the session runs the Codex CLI' }, 'Codex') : null, issue && on(issueForge(issue)) ? h('a', { class: 'chip link', href: issue.web_url, target: '_blank', rel: 'noopener', title: `${issue.title} - open it in ${forgeLabel(issue)}` }, `${issueRef(issue)} ↗`) : null, s.branch ? h('span', { class: 'chip dh-branch', title: 'git branch of the working tree' }, fi('branch', 'gi'), showBidi(s.branch)) : null, s.tty ? h('span', { class: 'chip', title: 'attached in a native terminal (piper attach): it sets the terminal size; Ctrl-] there detaches' }, '⌨ in a terminal') : null, iso, detail ? h('span', { class: 'chip warn', title: detail }, detail) : null), tabs, h('span', { class: 'grow' }), card?.flow && !contextShown() ? flowStrip(sessionFlow(card.flow, s)) : null, layoutControl(s, live), actions));
}
/** F03: the header's name - a title that only names the project shows its last segment (acme/acme-shop · 2 → acme-shop · 2) */
function headTitle(s) {
    const short = s.project.split('/').pop() || s.project;
    return s.title === s.project || s.title.startsWith(s.project + ' · ') ? short + s.title.slice(s.project.length) : s.title;
}
/** F03: one "Beside ▾"
 *  - Files ⌘O, Database ⌘D, HTTP, Host shell, Another session… / Grid ⌘G, Composer ⌘E and the grid's Layout presets;
 *  a dot / the number of panes on it says what is beside now. Then the context panel. */
function layoutControl(s, live) {
    const box = h('div', { class: 'layoutctl' });
    const open = grid && !zoomed() && view === 'term';
    const n = on('grid') ? (open ? gridMembers() : restoreMembers(layoutCtx())).length : 0;
    const bb = h('button', { class: 'beside' + (open ? ' on' : ''), 'aria-haspopup': 'menu', 'aria-expanded': String(!!layoutMenuEl),
        title: kbd(open ? `beside the terminal: ${n} panes in the grid (⌘G single)` : 'put Files, a Database, HTTP, a host shell or another session beside the terminal'),
        onclick: () => (layoutMenuEl ? closeBeside() : besideMenu(bb, s, live)) }, h('i', { class: 'fi fi-grid', 'aria-hidden': 'true' }), 'Beside', open && n > 1 ? h('span', { class: 'bcount' }, String(n)) : null, h('i', { class: 'chev fi fi-chev-d', 'aria-hidden': 'true' }));
    box.append(bb);
    box.append(h('button', { class: 'icon' + (inspector ? ' on' : ''), title: kbd(inspector ? 'hide the context panel (⌘I)' : 'show the context panel (⌘I)'),
        'aria-label': 'context panel', 'aria-pressed': String(inspector), onclick: toggleInspector }, h('i', { class: 'fi fi-panel-right', 'aria-hidden': 'true' })));
    return box;
}
/** 9.44 (a contributor's opt-in, 9.37 removed it; now always on): the tabs / toggles the session bar can hide - key, label,
 *  shortcut, available for `s`. Terminal always stays. */
function tabItems(s) {
    const proj = s.kind === 'project';
    return [['convo', 'Conversation', '', true], ['files', 'Files', '', proj], ['http', 'HTTP', '', proj && on('http')],
        ['history', 'History', '', true], ['composer', 'Composer in Beside ▾', '⌘E', on('composer')]]; // F03: the pane toggles are Beside ▾ itself
}
function loadHiddenTabs() {
    try {
        return new Set(JSON.parse(readStore('piper.tabs.hidden') ?? '[]'));
    }
    catch {
        return new Set();
    }
}
function setHiddenTabs(next) {
    hiddenTabs = next;
    writeStore('piper.tabs.hidden', JSON.stringify([...next]));
    render();
}
/** 9.44: right-click the session bar = tick what it shows (IDEA's toolbar menu); kept per browser */
function tabsMenu(e, s) {
    e.preventDefault();
    const flip = (k) => { const n = new Set(hiddenTabs); if (n.has(k))
        n.delete(k);
    else
        n.add(k); setHiddenTabs(n); };
    const item = ([k, label, keys]) => ({ label, keys, on: !hiddenTabs.has(k), run: () => flip(k) });
    const items = tabItems(s).filter(([, , , avail]) => avail);
    popMenu([
        { group: 'Session bar' },
        ...items.filter(([, , keys]) => !keys).map(item), // the views, then the toggles (they have a shortcut)
        null,
        ...items.filter(([, , keys]) => !!keys).map(item),
        null,
        { label: 'Show all', run: () => setHiddenTabs(new Set()), disabled: hiddenTabs.size === 0 ? 'every tab is shown' : false,
            title: kbd('a hidden tab stays reachable from the rail menu, ⌘K and its shortcut') },
    ], e.clientX, e.clientY, 'session bar');
}
/** the context panel is on screen (the workflow strip is there, not in the header). D8: no width threshold - the
 *  panel is a column at every width since N10, the 1100 px check only doubled the strip into the header below it */
function contextShown() {
    return inspector && !GLOBAL_VIEWS.has(view);
}
/** the isolation details live folded in the context panel: open it there */
function revealIsolation() {
    if (!inspector)
        toggleInspector();
    unfold('iso');
    insp.querySelector('.sec[data-k="iso"]')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
}
const VIEW_TITLE = {
    gitlab: ['GitLab', 'assigned issues, mentions, reviews and your merge requests - Enter on a row = its main action'],
    prod: ['Production', 'the morning check of errors and alerts in production'],
    usage: ['Usage', 'tokens per day and per project (from the transcripts of Piper sessions)'],
    timesheet: ['Timesheet', 'your time by issue and day - an overview only, nothing is sent'],
    settings: ['Settings', 'features, projects, sessions, version control, tools, isolation and Piper’s environment'],
    setup: ['Setup', 'what this machine needs, and which parts of Piper you see'],
    audit: ['Audit', 'security and operations records of piperd (audit.log)'],
    db: ['Database', 'connections, tables and the SQL console - the same connections Claude uses'],
};
function globalHeader() {
    const [title, sub] = view === 'setup' && state.features && !state.features.onboarded
        ? ['Welcome to Piper', 'a minute to set up, then your first session'] : view === 'gitlab' && inboxName(state.inbox) !== 'GitLab'
        ? [inboxName(state.inbox), on('gitlab') ? 'GitLab and GitHub: assigned issues, mentions, reviews, your MRs and PRs - Enter on a row = its main action'
                : 'assigned issues, mentions, reviews and your pull requests - Enter on a row = its main action']
        : VIEW_TITLE[view] ?? ['', ''];
    return h('header', { class: 'head' }, h('div', { class: 'titles' }, h('div', { class: 'title' }, title), h('div', { class: 'sub' }, sub)));
}
function statePanel(s) {
    if (s.state === 'blocked')
        return blockedPanel(s);
    if (['preflight', 'preparing', 'spawning', 'starting', 'attesting'].includes(s.state)) {
        const p = startupStatus(s);
        return h('section', { class: 'progress', 'aria-label': 'Session startup' }, h('div', { class: 'startup-head' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('div', { class: 'startup-heading' }, h('div', { class: 'startup-eyebrow' }, 'SESSION STARTUP'), h('strong', { role: 'status' }, p.title)), h('span', { class: 'startup-clock', 'data-since': String(p.since) }, startupElapsed(p.since) + ' in this step'), cancelStart(s)), h('p', { class: 'startup-hint' }, p.hint), h('ol', { class: 'startup-steps', 'aria-label': 'Startup stages' }, ...p.stages.map((label, i) => h('li', { class: 'step' + (i < p.step ? ' done' : i === p.step ? ' now' : ''),
            ...(i === p.step ? { 'aria-current': 'step' } : {}) }, h('span', { class: 'step-mark', 'aria-hidden': 'true' }, i < p.step ? '✓' : String(i + 1)), label))));
    }
    if (s.state === 'error') {
        const k18 = s.detail.includes('over-mount MISSING');
        return h('div', { class: 'panel bad' }, h('h2', {}, 'Error'), k18 ? h('p', {}, h('strong', {}, 'Docker did not hide the excluded directories (K18). '), 'Docker Desktop 4.90 detaches nested directory mounts shortly after the container starts, so excluded directories such as config/properties would be visible in the sandbox - so Piper did not let the session start. Files hidden via /dev/null work. The fix is on the Docker / hole side (piper/README.md, K18); until then run only projects without directory excludes, or use the Global agent.') : null, h('pre', {}, s.detail));
    }
    if (s.state === 'exited' && s.interrupted) { // 9.141: a restart stopped it, not the user
        const at = new Date(s.interrupted.at * 1000).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
        return h('div', { class: 'endbar interrupted', role: 'status' }, h('span', { class: 'dot st-intr', 'aria-hidden': 'true' }), h('span', {}, h('strong', {}, s.interrupted.cause === 'reboot' ? 'Interrupted by a restart' : 'Interrupted'), ' · ', wasWord(s.interrupted.was), ' · ', at, ' · sandbox gone, worktree and conversation kept'), h('span', { class: 'grow' }), h('button', { class: 'primary', onclick: () => void act(s, 'retry') }, continueLabel(s)), h('button', { onclick: () => setView('convo') }, 'Conversation'), h('button', { title: 'from the list (transcript and audit stay)', onclick: () => void act(s, 'forget') }, 'Remove'));
    }
    if (s.state === 'exited') {
        return h('div', { class: 'endbar', role: 'status' }, h('span', { class: 'dot st-exited', 'aria-hidden': 'true' }), h('span', {}, h('strong', {}, 'Session ended'), ' · sandbox destroyed · ', exitReason(s.rc), s.notice ? ' · ' + kbd(s.notice) : ''), h('span', { class: 'grow' }), h('button', { class: 'primary', onclick: () => void act(s, 'retry') }, continueLabel(s)), h('button', { onclick: () => setView('convo') }, 'Conversation'), h('button', { title: 'from the list (transcript and audit stay)', onclick: () => void act(s, 'forget') }, 'Remove'));
    }
    return null;
}
/** G1: the start progress has its own way out - piperd's stop() cancels a start at its next step */
function cancelStart(s) {
    return h('button', { class: 'mini', disabled: s.state === 'stopping', title: 'stop the start (the sandbox, if any, is destroyed)',
        onclick: () => void act(s, 'stop') }, 'Cancel');
}
const KIND_TEXT = {
    'settings-changed': 'changed hole settings - check the diff and approve',
    'settings-new': 'hole settings not approved in Piper yet - review them once and approve',
    'uncovered': 'sensitive files without an exclude - the sandbox would see them',
    'forbidden-key': 'forbidden key in the settings',
    'protected-mounted': 'a mount would expose a protected directory',
    'forbidden-port': 'Piper port in hostGatewayDomains',
    'scan-incomplete': 'scan incomplete (depth/symlinks)',
    'worktree-link-unverified': 'worktree link could not be verified',
    'settings-invalid': 'unreadable settings',
    'settings-legacy': 'a hole 1.x key - hole 2 will not start with it; replace it with network.allow (see the hole migration guide)',
};
/** N8: the blocked panel's own primary is "Approve and start" (only changed hole settings) */
function approvesInBody(s) { return s.blocks.every((b) => b.kind === 'settings-changed'); }
function blockedPanel(s) {
    const onlySettings = approvesInBody(s);
    const uncovered = s.blocks.filter((b) => b.kind === 'uncovered');
    const worktree = s.blocks.some((b) => b.kind === 'worktree-link-unverified');
    // group by kind, then by the root the finding belongs to (b.detail for uncovered)
    const groups = new Map();
    // a protected mount first: it is what keeps the start blocked whatever else gets approved
    for (const b of [...s.blocks].sort((x, y) => Number(y.kind === 'protected-mounted') - Number(x.kind === 'protected-mounted'))) {
        const key = blockGroupKey(b);
        groups.set(key, [...(groups.get(key) ?? []), b]);
    }
    const body = h('div', {});
    for (const [key, list] of groups) {
        const [kind, root] = key.split('|');
        const title = `${KIND_TEXT[kind] ?? kind} (${list.length})${root ? ' · ' + root : ''}`;
        const items = h('ul', { class: 'findings' }, ...list.slice(0, 200).map((b) => h('li', {}, h('code', {}, root && b.path.startsWith(root + '/') ? b.path.slice(root.length + 1) : b.path), b.kind === 'settings-changed'
            ? h('pre', { class: 'diff' }, ...(b.diff ?? '').split('\n').map((l) => h('span', {
                class: l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : '',
            }, l + '\n')))
            : b.detail && b.kind !== 'uncovered' ? h('span', { class: 'mut' }, ' ' + b.detail) : null)));
        body.append(h('details', { open: list.length <= 8 }, h('summary', {}, title), items));
    }
    const fix = async () => {
        try {
            const r = await api.fixExcludes(s.id);
            const d = r.data;
            toast(`Added: ${d?.written.join(', ') || 'nothing'}${d?.skipped.length ? ' · not possible: ' + d.skipped.join('; ') : ''}`);
            etag = '';
        }
        catch (e) {
            toast(String(e instanceof Error ? e.message : e));
        }
    };
    // nothing in this panel unblocks a protected mount: it goes first, loud, and says which mount to change
    const callouts = s.blocks.filter((b) => b.kind === 'protected-mounted').map((b) => {
        const t = protectedText(b);
        return h('div', { class: 'protected-callout' }, h('strong', {}, t.title), h('p', {}, t.what), h('p', {}, t.fix), h('p', { class: 'mut' }, 'Approve, Add excludes and Allow in this project cannot unblock this.'));
    });
    const allow = () => {
        const roots = [...new Set(uncovered.map((b) => b.detail ?? ''))].filter(Boolean);
        modal('Allow in this project', h('p', {}, `The agent will see ${uncovered.length === 1 ? 'this file' : `these ${uncovered.length} files`} in this and every later session of ${roots.join(', ') || 'this project'}. The isolation check accepts them only in those roots, including later sessions; other projects are still checked.`), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', { class: 'danger', onclick: async () => {
                closeModal();
                try {
                    const r = await api.allowUncovered(s.id);
                    toast(`Allowed: ${r.data?.allowed.join(', ') || 'nothing'}`);
                    etag = '';
                }
                catch (e) {
                    toast(String(e instanceof Error ? e.message : e));
                }
            } }, 'Allow')));
    };
    return h('div', { class: 'panel' }, h('h2', {}, 'Start blocked'), ...callouts, worktree ? h('div', { class: 'worktree-callout' }, h('p', {}, 'Git lists a worktree whose link Piper cannot verify. It may be stale (for example after temporary files were removed), moved or damaged. This check also applies in Minimal.'), h('p', {}, 'On the host, in this session’s repository, inspect the worktree records first. This command changes nothing:'), h('pre', {}, 'git worktree prune --dry-run --verbose'), h('p', {}, 'Only after verifying that every listed record is obsolete, run git worktree prune in that repository. If the worktree still exists or was moved, repair its links instead. Do not delete its files to dismiss this warning. Then Retry.'), h('p', { class: 'mut' }, 'Approving settings or adding excludes cannot fix a worktree link.')) : null, h('p', { class: callouts.length ? 'mut' : '' }, onlySettings
        ? 'The hole settings changed since the last approval (or this is the first start). Check the diff and approve.'
        : worktree && s.blocks.every((b) => b.kind === 'worktree-link-unverified')
            ? 'Start is blocked until the worktree links are valid.'
            : 'The preflight found files or settings that would let the sandbox see sensitive content.' + (uncovered.length
                ? ' "Add excludes" writes the missing paths to .hole/settings.json of the project/library (for a library also to .git/info/exclude, so they stay out of commits) and approves its own write; settings you had not approved yet still need your review. "Allow in this project" lets the agent see these files in the listed roots, including later sessions.'
                : callouts.length || worktree ? '' : ' Fix the settings file below, then Retry.')), body, h('div', { class: 'row' }, uncovered.length ? h('button', { onclick: () => void fix() }, `Add excludes (${uncovered.length})`) : null, !callouts.length && uncovered.length ? h('button', { title: 'the files are meant to be visible: accept them in this project only', onclick: allow }, `Allow in this project (${uncovered.length})`) : null, onlySettings ? h('button', { class: 'primary', onclick: () => act(s, 'approve') }, 'Approve and start') : null, worktree ? h('button', { class: 'primary', onclick: () => void act(s, 'retry') }, 'Retry') : null));
}
function renderInspector() {
    // a global view (GitLab, Audit, Usage…) is not about one session: the whole width is its
    const off = !inspector || GLOBAL_VIEWS.has(view) || !session(active); // no session: no empty column
    document.body.classList.toggle('no-inspector', off);
    // a poll rebuilds the inspector: keep where the user scrolled (folds: fold() and keepOpen() by key, W16)
    const scroll = insp.scrollTop;
    const sameSession = insp.dataset.session === active;
    // the Git panel & co. are the same elements after the rebuild: give the focus (and caret) back
    const focused = sameSession && document.activeElement instanceof HTMLElement && insp.contains(document.activeElement) ? document.activeElement : null;
    const caret = focused instanceof HTMLTextAreaElement || focused instanceof HTMLInputElement ? [focused.selectionStart, focused.selectionEnd] : null;
    insp.dataset.session = active;
    if (!sameSession)
        revealed.clear();
    clear(insp);
    window.queueMicrotask(() => {
        if (focused?.isConnected && document.activeElement !== focused) {
            focused.focus({ preventScroll: true });
            if (caret && caret[0] !== null) {
                try {
                    focused.setSelectionRange(caret[0], caret[1]);
                }
                catch { /* an input type without a caret */ }
            }
        }
        if (!sameSession)
            return;
        insp.scrollTop = scroll;
    });
    const s = session(active);
    if (!s || off)
        return;
    const att = state.gui_flags?.attestation === false || s.attest?.skipped ? null : s.attest;
    // 1. what needs you, 2. what you act on (Git, diagnostics, stack), 3. configuration, folded
    const needs = [];
    // 9.118: a waiting card is not repeated here - the card itself, the rail badge and the queue row show it
    if (att && !att.ok)
        needs.push(h('p', { class: 'bad' }, 'Isolation check failed:'), h('ul', { class: 'findings' }, ...att.failures.map((f) => h('li', {}, f))));
    if (state.gui_flags?.l5_watch !== false && s.watch.length) {
        const finding = (path) => h('li', { class: 'watch-path' }, h('code', { title: path }, path.startsWith(s.path + '/') ? path.slice(s.path.length + 1) : path));
        needs.push(h('p', {}, h('strong', {}, `New sensitive files (L5) · ${s.watch.length}. `), 'They appeared in this session’s mounted project or libraries after the start, so the sandbox sees them uncovered. Stop the session, add excludes and start it again.'), h('ul', { class: 'findings watch-preview' }, ...s.watch.slice(0, 3).map(finding)));
        if (s.watch.length > 3)
            needs.push(keepOpen(h('details', { class: 'watch-more' }, h('summary', {}, `Show ${s.watch.length - 3} more paths`), h('ul', { class: 'findings watch-findings' }, ...s.watch.slice(3).map(finding))), `watch:${s.id}`));
    }
    const detail = sessionDetail(s);
    const attention = s.state === 'attention' && detail && !detail.startsWith('L5:');
    if (attention)
        needs.push(h('p', {}, detail));
    if (needs.length)
        insp.append(h('section', { class: 'alert' }, h('h3', {}, att && !att.ok || attention ? 'Needs you' : 'Isolation warning'), ...needs));
    // every section below is one header line with its state; the body opens when there is something to do
    insp.append(...workSections(s));
    // N10-D-7: only a project session has a checkout - the morning check (prodcheck) neither (git crashed, CI 404)
    if (s.kind === 'project') {
        insp.append(git.el);
        git.show(s.id);
        if (on('diag')) {
            insp.append(diag.el);
            diag.show(s.id);
        }
        if (on('stack') && state.stack_up !== false) { // no stack provider answering: no stack to show
            insp.append(stack.el);
            stack.show(s.project);
        }
        if (s.kind === 'project' && on('db')) {
            insp.append(dbSection.el);
            dbSection.show(s.id);
        }
    }
    // W16: three sections of property rows (IntelliJ's Properties) instead of one heap under five sub-headings
    const net = s.network ?? {};
    const bit = (x) => h('span', { class: x.tone, title: x.title }, ' ' + x.text);
    const isoRows = s.kind === 'project' ? unlockProps(s) : [];
    const ownConfig = s.claude_mode === 'none' ? !s.host_codex : s.claude_mode === 'session';
    if (att || state.gui_flags?.attestation !== false || ownConfig || isoRows.length) {
        const iso = fold('iso', 'Isolation');
        iso.el.classList.add('iso');
        iso.status.append(...isoStatus(att, state.gui_flags?.unlocks_required === false ? undefined : { ...s.unlock, git: !state.gui_flags?.git_hole && !!s.unlock?.git }).map(bit));
        iso.body.append(...[
            att || state.gui_flags?.attestation !== false ? prop('Isolation check', att ? (att.ok ? `✓ ${att.checks} passed` : `✗ ${att.failures.length} of ${att.checks} failed`) : 'after the sandbox starts', att ? (att.ok ? 'ok' : 'bad') : 'mut', att?.ok ? 'Agent and DinD mounts verified.' : undefined) : null,
            ownConfig ? prop(s.claude_mode === 'none' ? '~/.codex' : '~/.claude', 'own (session)', 'mut') : null,
            ...isoRows,
        ].filter((x) => !!x));
        // a failed attestation or the advisor (no Git panel) shows it open
        iso.auto((!!att && !att.ok) || s.kind === 'advisor');
        insp.append(iso.el);
    }
    const ns = netStatus(net);
    const nf = fold('net', 'Network');
    nf.status.append(...ns.bits.map(bit));
    // refused requests first (the one thing here you act on); none yet is one row under Access
    const refused = net.denied?.length
        ? [h('div', { class: 'prop-h' }, 'Refused'), ...net.denied.map((d) => {
                const dom = deniedDomain(d);
                return prop(d, '', 'mut', undefined, s.kind === 'project' && dom
                    ? h('button', { class: 'mini', title: `add ${dom} to the project's network.allow`, onclick: () => confirmAllow(s, dom) }, 'Allow…') : null);
            })]
        : [];
    const stopped = ['blocked', 'error', 'exited'].includes(s.state);
    nf.body.append(...[
        ...refused,
        s.kind === 'project' ? h('div', { class: 'prop-h' }, 'Start options') : null,
        ...(s.kind === 'project' ? Object.keys(START_OPT_TEXT).map((k) => startOptRow(s, k)) : []),
        s.kind === 'project' ? h('p', { class: 'faint small' }, stopped ? 'Applies at the next start (Continue).' : 'Change them while the session is stopped, then Continue.') : null,
        h('div', { class: 'prop-h' }, 'Access'),
        // unfiltered by the setting for every session (not this session's start option): where to turn it off
        net.unrestricted && !s.opts?.unrestricted ? prop('Egress filter', 'off', 'warn', 'Network unrestricted (hole -u): the sandbox reaches anywhere, GitLab and internal services included. To turn it off: Settings › Sessions › Sandbox → Unfiltered network.', h('button', { class: 'mini lk', onclick: () => { settings.show('sandbox'); setView('settings'); } }, 'Settings')) : null,
        net.denied?.length ? null : prop('Refused', net.denied ? 'nothing' : 'listed when the session ends', 'mut'),
        net.unrestricted ? null : net.allow?.length
            ? keepOpen(h('details', { class: 'prop-list' }, h('summary', {}, h('span', {}, 'Allowed domains'), h('span', { class: 'mut' }, String(net.allow.length))), h('ul', { class: 'plain mono' }, ...net.allow.map((d) => h('li', {}, d)))), `allow:${s.id}`)
            : prop('Allowed domains', '0', 'mut'),
        net.gateway?.length ? prop('Host gateway', net.gateway.join(', '), 'mut') : null,
        // D13: the Global agent reads the GitLab of every project (sessions.py: the union) - "all projects", not a list
        on('gitlab') ? s.kind === 'advisor' ? prop('GitLab', s.gitlab.length ? `all projects · ${s.gitlab.length}` : 'none', 'mut', s.gitlab.length ? `${s.gitlab.join(', ')} - Piper tools use the host token; approval cards follow Settings › Security.` : 'No project has a GitLab repository.')
            : prop('GitLab', s.gitlab.length ? s.gitlab.join(', ') : 'none', 'mut', s.gitlab.length ? 'Piper tools use the host token; approval cards follow Settings › Security.' : 'No GitLab project (projects.json / origin).') : null,
        on('github') && (s.kind === 'project' || s.github?.length) ? prop('GitHub', s.github?.length ? s.github.join(', ') : 'none', 'mut', s.github?.length ? 'Piper tools use your host gh login; approval cards follow Settings › Security.' : 'No GitHub repository (projects.json / origin).') : null,
        s.gitlab_extra?.length ? prop('Also reads', s.gitlab_extra.join(', '), 'warn', 'allowed on a card, until the session ends; for every session: Settings › GitLab') : null,
    ].filter((x) => !!x));
    nf.auto(ns.open);
    insp.append(nf.el);
    const info = fold('info', 'Session');
    info.status.append(h('span', { title: s.instance ?? '' }, ' ' + sessionSummary(s.agent, s.instance, s.created))); // F13: words, the container below
    info.body.append(prop('Path', s.path, 'mut tail'), prop('Instance', s.instance ?? '—', 'mut'), prop(s.claude_mode === 'none' ? 'Codex session' : 'Claude session', midCut(s.claude_session ?? '—'), 'mut', s.claude_session ?? undefined), ...(s.switched_from ? switchedProps(s.switched_from) : []), prop('Created', new Date(s.created * 1000).toLocaleString(), 'mut'));
    info.auto(false);
    insp.append(info.el);
    // F13: Last turn right under Commit (what the turn changed beside what you commit)
    const turn = insp.querySelector('.sec[data-k="turn"]');
    if (turn && insp.contains(git.el))
        git.el.after(turn);
    else if (turn)
        insp.append(turn);
    insp.append(notInUse);
    syncNotInUse();
}
/** F13: a section of a module that is on but has nothing (Diagnostics no build, Stack none,
 *  Database none - `data-empty`, set by the section itself, also after a later fetch) leaves the panel for one
 *  line at its end: "Not in use: Diagnostics · Stack · Database"; a click brings that one back, opened */
const notInUse = h('p', { class: 'notinuse' });
const revealed = new Set();
function syncNotInUse() {
    const secs = [...insp.querySelectorAll(':scope > .sec')];
    for (const el of secs)
        el.classList.toggle('unused', el.hasAttribute('data-empty') && !revealed.has(el.dataset.k ?? ''));
    const hidden = secs.filter((el) => el.classList.contains('unused'));
    clear(notInUse);
    notInUse.hidden = !hidden.length;
    if (!hidden.length)
        return;
    notInUse.append('Not in use: ', ...hidden.flatMap((el, i) => {
        const k = el.dataset.k ?? '';
        return [i ? ' · ' : '', h('button', { class: 'link', title: 'show the section here', onclick: () => { revealed.add(k); unfold(k); syncNotInUse(); } }, el.querySelector('.sec-t')?.textContent ?? k)];
    }));
}
new MutationObserver(syncNotInUse).observe(insp, { subtree: true, attributes: true, attributeFilter: ['data-empty'] });
/** W23: a session's issue / PR lives on GitHub when its gl is "gh:owner/repo" */
function issueForge(i) {
    return i.gl.startsWith('gh:') ? 'github' : 'gitlab';
}
function forgeLabel(i) {
    return issueForge(i) === 'github' ? 'GitHub' : 'GitLab';
}
/** !12 for a GitLab MR, #12 for an issue and for a GitHub PR */
function issueRef(i) {
    return `${i.type === 'mr' && issueForge(i) === 'gitlab' ? '!' : '#'}${i.iid}`;
}
/** a long id as its ends (4f1c…9a2e); the whole one is the row's tooltip */
function midCut(x) {
    return x.length > 14 ? `${x.slice(0, 4)}…${x.slice(-4)}` : x;
}
/** W16: one property row - label · value · action; the explanation is the label's tooltip */
/** 9.140 (F29): the session whose work this one took over - Open while it is listed - and the worktree it worked in */
function switchedProps(f) {
    const src = session(f.session);
    const out = [prop('Continued from', `${agentName(f.agent)} · ${src?.title ?? f.title ?? f.session}`, 'mut', 'its conversation was handed over as piper-drop/handoff.md; it stays resumable', src ? h('button', { class: 'mini', onclick: () => select(f.session), title: 'open that session' }, 'Open') : null)];
    if (f.worktree)
        out.push(prop('Worktree', f.worktree + (f.branch ? ` · ${f.branch}` : ''), 'mut tail', 'where the previous agent last worked'));
    return out;
}
function prop(label, value, tone, title, action = null) {
    return h('div', { class: 'prop' }, h('span', { class: 'prop-l', title: title ?? label }, label), h('span', { class: 'prop-v ' + tone, title: value }, h('span', {}, value)), action ?? h('span', {}));
}
/** the issue (workflow, comment draft, status) and the last turn, placed at the panel's bottom -
 *  the prompt, the end of the answer and what the turn changed (the Git panel's turn view) */
function workSections(s) {
    const out = [];
    const card = boardData.get(s.id);
    const issue = card?.issue ?? s.issue ?? null;
    const flow = card?.flow ? sessionFlow(card.flow, s) : null;
    const ist = card?.istate ?? null;
    if ((issue || flow) && on(issue ? issueForge(issue) : 'gitlab')) {
        const ref = issue ? `${issue.type === 'mr' ? (issueForge(issue) === 'github' ? 'PR ' : 'MR ') : 'Issue '}${issueRef(issue)}` : 'Workflow';
        const sec = fold('issue', issue ? h('span', { title: issue.title }, ref) : ref);
        if (issue) {
            sec.actions.append(h('button', { class: 'mini', title: `open it in the ${inboxName(state.inbox)} inbox`, onclick: () => openGitlab(issue.key) }, 'Inbox'), h('a', { class: 'mini', href: issue.web_url, target: '_blank', rel: 'noopener', title: `open it in ${forgeLabel(issue)}` }, `${forgeLabel(issue)} ↗`));
            // the header of the detail shows the session title: the issue title only when it says more
            if (!s.title.includes(issue.title))
                sec.body.append(h('p', { class: 'wk-issue' }, issue.title));
        }
        if (flow)
            sec.body.append(flowBar(flow));
        if (flow?.draft && !flow.draft.sent) {
            sec.body.append(h('button', { class: 'cdraft', title: flow.draft.path, onclick: () => inbox.openDraft(s.id, issue) }, '✎ Comment draft ready - review and send'));
        }
        // a live session gets the button only at its prompt; an ended one gets piperd's summary
        if (ist?.stale && (!ist.live || ist.prompt))
            sec.body.append(h('p', {}, istateButton(s, ist)));
        sec.auto(true);
        out.push(sec.el);
    }
    const asked = askedText(card?.asked || s.task);
    const answer = card?.last ? lastParagraph(card.last) : '';
    const hasGit = s.kind !== 'advisor';
    if (!s.hold && !asked && !answer && !hasGit)
        return out;
    const sec = fold('turn', 'Last turn');
    if (hasGit)
        sec.status.append(git.turn.status);
    if (s.hold) { // 9.12: automatic text (a handoff line) waits: the human typed since the last prompt
        sec.body.append(h('p', { class: 'warn small' }, `⌨ ${s.hold} `, h('button', {
            class: 'link small', title: 'the prompt in the terminal is empty (check it): Piper types the waiting text now',
            onclick: () => void api.inputClear(s.id).catch((e) => toast('Not sent: ' + String(e instanceof Error ? e.message : e))),
        }, 'Send now')));
    }
    if (asked)
        sec.body.append(h('p', { class: 'wk-row' }, h('span', { class: 'wk-k' }, 'You'), h('span', { class: 'wk-asked' }, showBidi(asked.split('\n')[0].slice(0, 240)))));
    if (answer)
        sec.body.append(h('p', { class: 'wk-row' }, h('span', { class: 'wk-k' }, 'Claude'), h('span', { class: 'wk-last', title: showBidi(answer) }, showBidi(answer))));
    if (asked || answer)
        sec.body.append(h('button', { class: 'link small', onclick: () => setView('convo') }, 'The whole conversation →'));
    if (hasGit)
        sec.body.append(git.turn.body);
    // Always folded by default; only the user's manual toggle opens it (kept across polls).
    sec.auto(false);
    out.push(sec.el);
    return out;
}
const git = createGitPanel({
    toast, modal, closeModal, modalOver, closeAllModals,
    session,
    sessionCreated: (id) => { void poll().then(() => { select(id); setView('term'); }); },
    openSourceFull: (id, path, line) => {
        if (id !== active)
            select(id);
        setView('files');
        files.openFile(session(id)?.project ?? '', path, line);
    },
    conversation: (id, index) => {
        if (id !== active)
            lastActive = active;
        active = id;
        view = 'convo';
        convo.show(id, index);
        render();
    },
    projectPath: (id) => session(id)?.path ?? '',
    send: (id, text) => sendText(id, text),
    openFile: (project, path, line) => openBeside(project, path, line),
    onNotes: () => files.refreshNotes(),
    openSession: (id) => { closeAllModals(); select(id); setView('term'); },
    newSession: (project) => { closeAllModals(); newSessionDialog(false, undefined, project); },
    sharedWith: (id) => {
        const me = session(id);
        return me ? state.sessions.filter((x) => x.id !== id && x.kind === 'project' && x.path === me.path && !x.hidden && LIVE.has(x.state)).map((x) => x.title) : [];
    },
    workingIn: (id) => {
        const me = session(id);
        return me ? state.sessions.filter((x) => x.kind === 'project' && x.path === me.path && !x.hidden && x.state === 'working').map((x) => x.title) : [];
    },
    requireReview: () => !!state.gui_flags?.commit_requires_review,
    onStatus: () => files.refreshMarks(),
    project: (id) => session(id)?.project ?? '',
    gitlab: (id) => on('gitlab') && (session(id)?.gitlab.length ?? 0) > 0,
    ciEnabled: (forge) => on(forge),
    onCi: (project, ci) => {
        const was = ciSeen.get(project);
        const now = ci.pipeline ? `${ci.pipeline.id}:${ci.pipeline.status}` : '';
        ciSeen.set(project, now);
        // a pipeline that turned failed while you watched (not the one already failed at first sight)
        if (was !== undefined && was !== now && ci.pipeline?.status === 'failed') {
            const s = state.sessions.find((x) => x.project === project && !x.hidden);
            notify(`Pipeline failed: ${project}`, `${ci.branch} · #${ci.pipeline.id}`, s?.id ?? active);
            toast(`Pipeline ${ci.branch} in ${project} failed`);
        }
    },
});
const ciSeen = new Map();
const diag = createDiag({
    toast, modal, closeModal,
    project: (id) => session(id)?.project ?? '',
    openFile: (project, path, line) => openBeside(project, path, line),
    send: (id, text) => sendText(id, text),
});
const stack = createStack({ toast, modal, closeModal });
const dbSection = createDbSection({ pending: () => state.pending, open: (connection) => openDb(connection), openPane: (connection) => openDbPane(connection) }); // 9.37: the beside pair needs no grid module
const usage = createUsage();
const hostView = createHostView({
    state: () => state,
    makeTerm: (id, cwd) => new TermView(id, cwd, {
        onRefused: (why) => toast(`Host shell: ${why}`),
        onUrl: (uri) => sessionUrl(uri),
        onExit: () => undefined,
        onConnection: () => undefined,
        onImage: () => Promise.reject(new Error('an image goes to a session, not to a host shell')),
    }),
    currentProject: () => { const s = session(active); return s?.kind === 'project' ? s.project : ''; },
    openSettings: () => { settings.show('features'); setView('settings'); },
    toast,
    changed: () => { etag = ''; renderActbar(); },
    grid: {
        has: (id) => gridMembers().includes(id),
        toggle: (id) => {
            if (!on('grid'))
                return;
            const inGrid = gridMembers().includes(id);
            if (!inGrid && gridMembers().length >= GRID_MAX) {
                toast(`The grid has ${GRID_MAX} panes at most`);
                return;
            }
            gridEdit((l) => (inGrid ? l.filter((x) => x !== id) : [...l, id]));
            if (!inGrid)
                toast('In the grid - ⌘G in the terminal view shows it');
        },
    },
});
window.setInterval(() => hostView.closeStale(), 60_000); // D5: an ended shell outside the grid goes after 10 minutes
const timesheet = createTimesheet({ toast, modal, closeModal, openSettings: (sec) => { settings.show(sec); setView('settings'); } });
const settings = createSettings({ toast, modal, closeModal, openRules: () => inbox.openRules(), openSetup: () => setView('setup'),
    openHttpEnv: () => {
        const s = session(active);
        const p = s?.kind === 'project' ? s.project : '';
        if (p)
            setView('http');
        void httpPanel.openEnvironments(p);
    },
    changed: () => { etag = ''; }, advanced: () => state.features?.preset !== 'basic', features: () => state.features });
const setup = createSetup({
    toast,
    openSettings: (sec) => { settings.show(sec); setView('settings'); },
    changed: () => { etag = ''; },
    features: () => state.features,
    applied: (f) => {
        state = { ...state, features: f };
        syncFeatures();
        render();
    },
    done: () => {
        setup.dismiss();
        if (view === 'setup')
            setView('term');
        else
            render();
    },
    newSession: () => newSessionDialog(),
    rebuild: () => rebuildState(),
    setRebuild: (on) => setRebuild(on),
});
/** 9.54: the pending image rebuild and the title of the start that took it */
function rebuildState() {
    const r = state.image_rebuild;
    const s = r?.session ? session(r.session) : undefined;
    return { pending: !!r?.at, by: s && s.rebuild && ['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting'].includes(s.state) ? s.title : '' };
}
/** armed and not yet taken by a start that is still building: the next start takes it (a start during a build does not) */
function rebuildArmed() {
    const r = rebuildState();
    return r.pending && !r.by;
}
async function setRebuild(on) {
    try {
        const r = await api.imageRebuild(on);
        state = { ...state, image_rebuild: r.data?.image_rebuild ?? {} };
        etag = '';
        toast(on ? 'The next session start rebuilds the sandbox image (several minutes)' : 'Image rebuild canceled');
        render();
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
    }
}
window.addEventListener('beforeunload', (e) => {
    if (settings.dirty() || dbView.hasUnsaved() || [...dbPanes.values()].some((p) => p.view.hasUnsaved()))
        e.preventDefault();
});
// K2: a project without a session still shows its git state in Files (GET /api/git/status?project=)
const PROJ_GIT_MS = 10000;
const projGit = new Map();
function projectGit(project) {
    let e = projGit.get(project);
    if (!e) {
        e = { at: 0, busy: false, failed: false, marks: new Map(), branch: '' };
        projGit.set(project, e);
    }
    const ent = e;
    if (!ent.busy && Date.now() - ent.at > PROJ_GIT_MS) {
        ent.busy = true;
        void projectGitStatus(project).then((r) => {
            const st = r.data;
            if (!st)
                return;
            const path = state.projects.find((p) => p.key === project)?.path ?? '';
            const marks = new Map();
            for (const f of st.files) {
                const p = st.root && path ? projectRel(f.path, st.root, path) : f.path;
                if (!p.startsWith('/'))
                    marks.set(p, changeMark(f.x + f.y));
            }
            const moved = JSON.stringify([...marks]) !== JSON.stringify([...ent.marks]) || st.branch !== ent.branch;
            ent.marks = marks;
            ent.branch = st.branch;
            ent.failed = false;
            if (moved)
                files.refreshMarks();
        }).catch((e) => {
            if (!(e instanceof ApiError && e.status === 404) && !ent.failed)
                toast(`Git status of ${project}: ${String(e instanceof Error ? e.message : e)}`);
            ent.failed = true;
        })
            .finally(() => { ent.at = Date.now(); ent.busy = false; });
    }
    return ent;
}
const files = createFiles({
    notice: (text) => { notices.add(text, 'files'); bellSoon(); },
    nav: (dir) => { if (navOn()) {
        if (dir < 0)
            history.back();
        else
            history.forward();
    } },
    toast, modal, closeModal,
    showFiles: (project) => { setView('files'); files.show(project); },
    projects: () => state.projects,
    insert: (text) => insertRef(text),
    changed: (project) => {
        const s = projectSession(project);
        return s ? git.changed(s.id) : new Set(projectGit(project).marks.keys());
    },
    diff: (project, path) => {
        const s = projectSession(project);
        if (s)
            git.diffOf(s.id, path);
    },
    addNote: (project, path, line, text, excerpt) => addNote(project, path, line, text, excerpt),
    notes: (project) => { const s = projectSession(project); return s ? git.notes(s.id) : []; },
    send: (project, text) => { const s = projectSession(project); return s ? sendText(s.id, text) : Promise.resolve(false); },
    diagOn: () => on('diag'),
    history: (project, path) => openHistory({ toast, modal, closeModal, openFile: openBeside }, project, path),
    reviewPaths: project => { const s = projectSession(project); return s ? git.reviewPathsOf(s.id) : []; },
    reviewLeave: async (project, path, sha, leftOid, leftMode) => { const s = projectSession(project); if (s)
        await git.viewedOf(s.id, path, sha, leftOid, leftMode); },
    stageFile: (project, path) => { const s = projectSession(project); if (s)
        git.stageOf(s.id, path); },
    reviewTarget: (project, target) => { const s = projectSession(project); if (s)
        git.actionsOf(s.id, target);
    else
        toast('Select a project session to collect review actions.'); },
    // saving from the GUI would race a Claude that is editing right now
    working: (project) => state.sessions.some((x) => x.kind === 'project' && x.project === project && !x.hidden && x.state === 'working'),
    marks: (project) => { const s = projectSession(project); return s ? git.marks(s.id) : projectGit(project).marks; },
    turnFiles: (project) => { const s = projectSession(project); return s ? git.pickedTurnFiles(s.id) : null; },
    branch: (project) => { const s = projectSession(project); return s ? s.branch || git.branch(s.id) : projectGit(project).branch; },
    sessionOf: (project) => projectSession(project)?.id ?? '',
    gitChanged: (project) => { const s = projectSession(project); if (s)
        git.invalidate(s.id); },
    commands: () => commands().filter((c) => !c.label.startsWith('File')),
});
document.addEventListener('visibilitychange', () => { if (document.hidden)
    files.flush(); }); // 9.41
window.addEventListener('beforeunload', (e) => {
    if (files.dirty() || git.dirty())
        e.preventDefault();
});
/** the project's session the file browser works with: the active one when it is of that project, else the first live */
function projectSession(project) {
    const a = session(active);
    if (a?.kind === 'project' && a.project === project && !a.hidden)
        return a;
    return state.sessions.find((x) => x.kind === 'project' && x.project === project && !x.hidden && LIVE.has(x.state))
        ?? state.sessions.find((x) => x.kind === 'project' && x.project === project && !x.hidden);
}
const dbView = createDbView({
    toast, modal, closeModal,
    projects: () => state.projects.filter((p) => p.exists).map((p) => ({ key: p.key, title: p.title || p.key })),
    pending: () => state.pending,
});
/** D1: the grid shows `pane` (Files / the active project's Database) on the Terminal tab */
function paneShown(pane) {
    return grid && view === 'term' && gridIdsShown().includes(pane);
}
function filesPaneShown() {
    return paneShown(FILES_PANE);
}
/** D1: the Files pane next to the active session (a click on a path, ⌘P, back / forward); where the grid cannot
 *  have it (a full grid, no project session) the Files tab; 9.37: also without the grid module (the beside pair) */
function showFilesPane() {
    if (filesPaneShown())
        return;
    const s = session(active);
    if (s?.kind !== 'project' || (grid && !gridMembers().includes(FILES_PANE) && gridMembers().length >= GRID_MAX)) {
        setView('files');
        return;
    }
    dispatch({ t: 'pane', pane: FILES_PANE });
}
/** D8/D1: the context panel's "Open pane" - the project's Database pane next to the session, on `connection` */
function openDbPane(connection) {
    if (!paneShown(DB_HERE))
        dispatch({ t: 'pane', pane: DB_HERE });
    if (paneShown(DB_HERE) && connection)
        dbPane(DB_HERE).view.show(connection);
}
/** D1: the grid's Files pane - the Files view's browser in it, on the active session's project once per switch */
function filesPane() {
    if (files.el.parentElement !== filesPaneEl)
        filesPaneEl.append(files.el);
    const s = session(active);
    if (filesFor !== active) {
        filesFor = active;
        if (s?.kind === 'project' && files.project() !== s.project)
            files.show(s.project);
    }
    return filesPaneEl;
}
/** 9.42: the grid's HTTP pane - the HTTP tab's panel in it, on the active session's project once per switch */
function httpPane() {
    if (httpPanel.el.parentElement !== httpPaneEl)
        httpPaneEl.append(httpPanel.el);
    const s = session(active);
    if (httpFor !== active) {
        httpFor = active;
        if (s?.kind === 'project')
            httpPanel.show(s.project);
    }
    return httpPaneEl;
}
/** the Database view: every connection of every project, tied to no session; `connection` picks one */
function openDb(connection = '') {
    if (view !== 'db')
        setView('db');
    dbView.show(connection);
    render();
}
const httpPanel = createHttpPanel({
    toast, modal, closeModal,
    openFile: (project, path, line) => { setView('files'); files.openFile(project, path, line); },
});
const convo = createConvo({
    toast, modal, closeModal,
    onReference: (id, ref) => { const s = session(id); if (s)
        linkTask(openReference(s, ref)); },
    openFile: (id, path, line) => {
        const s = session(id);
        if (!s || s.kind !== 'project')
            return;
        if (path.startsWith('/?')) {
            sessionUrl(path, s);
            return;
        }
        const rel = path.startsWith(s.path + '/') ? path.slice(s.path.length + 1) : path.startsWith('/') ? '' : path.replace(/^\.\//, '');
        if (rel)
            linkTask(openReference(s, { kind: 'file', path: rel, line }));
    },
    resumeConversations: () => showResume(),
    jump: (id, turn) => {
        if (id !== active)
            lastActive = active;
        active = id;
        view = 'convo';
        convo.show(id, turn);
        render();
    },
});
const inbox = createInbox({
    sessions: () => state.sessions,
    projects: () => state.projects,
    label: (st) => LABEL[st] ?? st,
    select,
    openSession: (id) => {
        etag = '';
        if (session(id))
            select(id);
        else
            pendingSelect = id;
    },
    kick: () => { etag = ''; },
    started: rememberStarted,
    toast: (text, action) => toast(text, action),
    notify,
    modal,
    closeModal,
    editTask: (p) => newSessionDialog(false, p),
    showBoard: () => { if (view !== 'gitlab')
        setView('gitlab'); },
    openSettings: (section) => {
        settings.show(section);
        if (view !== 'settings')
            setView('settings');
    },
    boardVisible: () => view === 'gitlab',
    back: () => focusQueue(),
});
const prod = createProd({
    select,
    openSession: (id) => {
        etag = '';
        if (session(id))
            select(id);
        else
            pendingSelect = id;
    },
    kick: () => { etag = ''; },
    toast: (text) => toast(text),
    notify,
    showBoard: () => { if (view !== 'prod')
        setView('prod'); },
    openSettings: (section, filter) => {
        settings.show(section, filter);
        if (view !== 'settings')
            setView('settings');
    },
});
// 9.14 Feed: in the drawer (⌘0) only its chat with the Global agent; its actions and events are the queue's
const feed = createFeed({
    select,
    openInbox: (key) => openGitlab(key),
    openProd: () => setView('prod'),
    decide: async (id, ok, always, sessionAlways) => {
        await api.decide(id, ok, always, sessionAlways);
        etag = '';
    },
    reply: (id, text) => sendText(id, text),
    toast: (text) => toast(text),
    shown: () => drawer && !document.hidden,
    back: () => toggleDrawer(false),
    title: (id) => {
        const s = session(id);
        return s ? (s.kind === 'advisor' ? 'Global agent' : s.title) : id;
    },
    chatOnly: true,
    changed: () => queue.render(),
});
// F21: the agent's state is said once - here, "Agent ● ready"
const drawerState = h('span', { class: 'dr-state' });
function drawDrawerState() {
    const st = feed.status();
    drawerState.className = 'dr-state' + (st.live ? ' live' : '');
    drawerState.textContent = markBidi(st.text);
}
// the drawer: over the detail, the queue stays visible and usable beside it
const drawerEl = h('aside', { id: 'drawer', class: 'drawer hidden', role: 'dialog', 'aria-label': 'Global agent chat' }, h('div', { class: 'drawer-head' }, h('h2', {}, 'Agent'), drawerState, h('span', { class: 'grow' }), h('button', { class: 'ghost', title: 'the agent\'s own terminal', onclick: () => {
        const adv = state.sessions.find((s) => s.kind === 'advisor' && LIVE.has(s.state));
        if (!adv)
            return toast('The Global agent is not running');
        toggleDrawer(false);
        select(adv.id);
    } }, 'Terminal'), h('button', { class: 'icon', title: kbd('close (Esc, ⌘0)'), 'aria-label': 'close', onclick: () => toggleDrawer(false) }, fi('x', 'gi'))), feed.el);
document.body.append(drawerEl, zenPill);
function toggleDrawer(on = !drawer) {
    if (on && !state.feed?.on)
        return openAgent();
    dispatch({ t: 'agent', on }); // I7: the right column; closing gives it back to what was there
}
/** the drawer's DOM follows `drawer` (after a dispatch) */
function drawDrawer(focus) {
    if (drawerEl.classList.contains('hidden') === !drawer)
        return;
    drawerEl.classList.toggle('hidden', !drawer);
    document.body.classList.toggle('drawer-open', drawer); // the floating confirmations move beside it
    renderActbar();
    if (drawer) {
        feed.show(false);
        drawDrawerState();
    }
    if (!focus)
        return;
    if (drawer)
        window.requestAnimationFrame(() => feed.focusInput());
    else
        focusDetail();
}
// GitLab and Production: the full views of the queue's filters (and of its New items)
const prodOff = h('p', { class: 'mut view-off hidden' }, 'The production check is off: map a project to its observability project first. ', h('button', { class: 'link', onclick: () => { settings.show('prodcheck'); setView('settings'); } }, 'Settings → Production'));
const gitlabView = h('div', { class: 'board gl-view' }, inbox.el);
const prodView = h('div', { class: 'board prod-view' }, prodOff, prod.el);
const queue = createQueue({
    dangerOf: (id) => { const p = state.pending.find((x) => x.id === id); return p ? dangerLabel(p) : ''; },
    fold: () => toggleStrip(),
    foldIcon: () => icon('panel-close'),
    extra: () => limits.el,
    input: () => ({
        sessions: state.sessions,
        actions: state.queue?.actions ?? [],
        events: state.feed?.on ? feed.data().events ?? [] : [],
        seen: state.feed?.on ? feed.data().seen ?? 0 : 0,
        board: boardData,
        unseen,
        pins: railPins,
        order: railOrder,
        label: (st) => LABEL[st] ?? st,
        gone: state.pending_gone ?? [],
    }),
    feedOn: () => !!state.feed?.on,
    legacy: () => !state.queue && state.sessions.length > 0,
    dismissed: () => state.queue?.dismissed ?? 0,
    selected: isSelected,
    open: openItem,
    canReply: (sid) => { const s = session(sid); return !!s && s.pid !== null && ['waiting', 'attention', 'ready', 'working'].includes(s.state); },
    numberOf: (sid) => sessionIds().indexOf(sid),
    orderIds: () => sessionIds(),
    reply: (sid) => replyTo(sid),
    decide: async (id, ok, always, sessionAlways) => {
        try {
            await api.decide(id, ok, always, sessionAlways);
        }
        catch (e) {
            if (decidedElsewhere(e))
                return; // answered meanwhile: no error, the row goes with the next poll
            toast('Not decided: ' + String(e instanceof Error ? e.message : e));
            throw e;
        }
        finally {
            etag = '';
        }
    },
    restoreTrash: async (project, entries) => {
        const done = [];
        const failed = [];
        for (const t of entries) { // one at a time: each is its own guarded write, a refusal names its reason
            try {
                await api.fsRestore(project, t.id, true);
                done.push(t.path);
            }
            catch (e) {
                failed.push({ path: t.path, error: String(e instanceof Error ? e.message : e) });
            }
        }
        etag = ''; // the row shrinks or goes with the next poll
        toast(restoreToast(done, failed));
    },
    dismiss: (it) => {
        const a = it.action;
        if (!a)
            return;
        if (state.feed?.on || allowedFailed(a)) {
            void feedApi.dismiss(a.key).then(() => { etag = ''; if (state.feed?.on)
                feed.reload(); }).catch((e) => toast(String(e instanceof Error ? e.message : e)));
        }
        else if (a.source === 'gitlab') {
            void api.inboxSeen([a.subject.key]).then(() => { etag = ''; }).catch((e) => toast(String(e instanceof Error ? e.message : e)));
        }
        else
            toast('Dismissing needs the Feed (Settings → Feed); a session item goes when it is answered');
    },
    undismiss: () => void feedApi.undismiss().then((r) => { etag = ''; feed.reload(); toast(`Restored: ${r.data?.restored ?? 0}`); })
        .catch((e) => toast(String(e instanceof Error ? e.message : e))),
    muted: () => {
        const m = state.feed?.on ? feed.data().muted ?? {} : {};
        const now = Date.now() / 1000;
        return Object.entries(m).filter(([, until]) => until > now).map(([key]) => ({ key, label: session(key)?.title ?? key }));
    },
    unmute: (keys) => void Promise.all(keys.map((k) => feedApi.mute(k, 0))).then(() => { etag = ''; feed.reload(); })
        .catch((e) => toast(String(e instanceof Error ? e.message : e))),
    mute: (it) => {
        const subj = it.action?.subject;
        if (!subj)
            return;
        void feedApi.mute(subj.key, 60).then(() => { etag = ''; feed.reload(); toast(`${subj.label || subj.key}: muted in the queue for an hour`); })
            .catch((e) => toast(String(e instanceof Error ? e.message : e)));
    },
    handOver: (subj) => {
        toggleDrawer(true);
        feed.handOver(subj);
    },
    // D8 (R14): Continue / Retry does what it says - resume or start again, no dialog; Find & resume… has the dialog
    continueSession: (sid) => { const s = session(sid); if (s)
        void act(s, 'retry'); },
    restoreInterrupted: () => restoreInterrupted(),
    dismissInterrupted: () => void call('POST', '/api/interrupted', { dismiss: 'all' }).then(() => { etag = ''; })
        .catch((e) => toast(String(e instanceof Error ? e.message : e))),
    rebuildPending: () => rebuildArmed(),
    resumeConversations: (sid) => showResume(sid),
    forget: (sid) => { const s = session(sid); if (s)
        void act(s, 'forget'); },
    menu: (sid, x, y) => { const s = session(sid); if (s)
        openRailMenu(s, x, y); },
    saveOrder: (ids) => saveRailOrder(ids),
    newSession: () => newSessionDialog(),
    palette: () => palette(),
    back: () => focusDetail(),
    shortcuts: () => shortcuts(),
    openSettings: (sec) => { settings.show(sec); setView('settings'); },
    markSeen: (ts) => void feedApi.seen(ts).then(() => { etag = ''; }).catch(() => undefined),
});
document.getElementById('queue').replaceWith(queue.el);
queue.el.id = 'queue';
queue.el.after(qsplit);
document.body.append(qpeek);
/** GET /api/board: the queue's second lines, the detail's header and context (every 5 s, not hidden) */
let boardSig = '';
let boardBusy = false;
let boardFailures = 0;
async function loadBoard() {
    if (boardBusy || !token())
        return;
    boardBusy = true;
    try {
        const r = await api.board();
        const cards = r.data?.cards ?? [];
        const sig = JSON.stringify(cards);
        if (sig !== boardSig) {
            boardSig = sig;
            boardData = new Map(cards.map((c) => [c.id, c]));
            render();
        }
        boardFailures = 0;
    }
    catch {
        // A brief disconnect (including a daemon restart) is retried silently.
        // These are optional queue previews, not the conversation itself.
        if (++boardFailures === 3)
            toast('Session previews are temporarily unavailable. Retrying automatically.');
    }
    finally {
        boardBusy = false;
    }
}
const audit = createAudit({
    titleOf: (id) => {
        const s = session(id);
        return s ? (s.kind === 'advisor' ? 'Global agent' : s.title) : id;
    },
    select,
});
const composer = createComposer({
    current: () => active,
    title: (id) => {
        const s = session(id);
        return s ? (s.kind === 'advisor' ? 'Global agent' : s.title) : id;
    },
    sendTo: (id, text) => sendText(id, text),
    busy: (id) => session(id)?.state === 'working',
    live: (id) => sendable(session(id)),
    closed: () => {
        if (view === 'convo')
            composerDismissed = true;
        composerAuto = false;
        if (view === 'term')
            terms.get(active)?.focus();
        renderMain();
    },
    others: (id) => state.sessions.filter((s) => s.id !== id && s.kind === 'project' && !s.hidden && ['ready', 'working', 'waiting', 'attention'].includes(s.state))
        .map((s) => ({ id: s.id, title: s.kind === 'advisor' ? 'Global agent' : s.title })),
    confirm: (title, text, ok) => modal(title, h('pre', { class: 'confirm' }, text), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', { class: 'primary', onclick: () => { closeModal(); ok(); } }, 'Send'))),
    toast,
});
main.append(headEl, bodyEl, composer.el);
async function sendText(id, text) {
    try {
        await api.send(id, text);
        return true;
    }
    catch (e) {
        toast('Not sent: ' + String(e instanceof Error ? e.message : e));
        return false;
    }
}
/** open a project file in the Files pane next to the terminal (D1), or in Files from another view */
function openBeside(project, path, line = 0) {
    if (view === 'term')
        showFilesPane();
    else
        setView('files');
    files.show(project);
    files.open(path, line);
}
/** a review note typed in the file preview: it joins the Git panel's notes of the project's session */
function addNote(project, path, line, text, excerpt) {
    const s = projectSession(project); // the session whose Git panel Files shows (K6)
    if (!s)
        return false;
    git.addNote(s.id, { path, line, old: false, text, ...(excerpt ? { excerpt } : {}) });
    return true;
}
/** a file reference from the browser into the active session's prompt (text only, no content) */
function insertRef(ref) {
    const s = session(active);
    const t = s ? terms.get(s.id) : undefined;
    if (!s || !t || !LIVE.has(s.state))
        return false;
    const project = files.project();
    if (s.kind === 'project' && s.project !== project)
        return false;
    setView('term');
    t.insert(s.kind === 'advisor' ? `${project}:${ref}` : ref);
    return true;
}
/** DB "→ Claude": into the active session's Composer; 9.44 the Composer module off - pasted into its terminal (no Enter) */
function toSession(text) {
    if (on('composer'))
        return composer.put(text);
    const t = terms.get(active);
    if (!t || !sendable(session(active)))
        return toast('No running session to paste into');
    if (toTerm())
        t.insert(text);
}
/** Reply r in the queue (D7): the Composer collapsed and pinned to that session, under the terminal or Conversation;
 *  9.44 the Composer module off: that session's terminal, focused */
function replyTo(sid) {
    if (!sendable(session(sid)))
        return;
    if (!on('composer'))
        return select(sid);
    const go = () => {
        composer.replyTo(sid);
        composerAuto = false;
        renderMain();
    };
    if (view === 'term' || view === 'convo' || toTerm(go))
        go();
}
/** ⌘E / the header toggle (D7): closed → full, collapsed → full, full → closed (in Conversation: collapsed) */
function toggleComposer(on) {
    const s = session(active);
    if (!s || !LIVE.has(s.state))
        return toast('Composer is for a live session');
    if (view === 'convo') {
        if (composer.open() && !composer.compact())
            composer.setCompact(true);
        else
            composer.toggle(true, { compact: false });
        composerAuto = false; // the user's own now: it stays when the view changes to the terminal
        renderMain();
        return;
    }
    if (on !== false && composer.open() && composer.compact()) {
        composer.setCompact(false);
        renderMain();
        return;
    }
    if (!toTerm(() => toggleComposer(on)))
        return;
    const t = terms.get(active);
    t?.freeze(true);
    composer.toggle(on, { compact: false });
    if (!composer.open())
        t?.focus(); // N10-D-9: closed - the keyboard back to the terminal (it fell to <body>)
    renderMain();
    window.setTimeout(() => t?.freeze(false), 150);
}
const UNLOCK_TEXT = {
    projects: {
        label: 'Reading other projects',
        on: 'The sandbox reads every checkout in PROJECT_PATH (listing, reading and searching files).',
        warn: [
            'The agent will see the sources of every project in PROJECT_PATH (other clients too) and can carry them into this project or into its answer.',
            'Read only, through piperd on the host; in every project its own hidden files apply (.git, config/properties, keys…).',
        ],
    },
    projects_write: {
        label: 'Editing other projects',
        on: 'The sandbox may propose file changes and new files in the other checkouts; you confirm each one on a card with a diff. Includes reading.',
        warn: [
            'The agent will propose changes and new files in other projects on the host - code and build scripts too, which you or another session then run. You see every change as a diff and it is written only when you allow it.',
            'Still refused: its own project, .git or that project\'s hidden files, anything through a symlink, and a project another session is working in. Files that run by themselves (.envrc, .vscode, build scripts…) are marked on the card.',
        ],
    },
    db_write_other: {
        label: 'Writing other projects\' databases',
        on: 'The session may write to a database connection of another project (the agent named it and you allowed reading it on a card); each write is still confirmed on a card.',
        warn: [
            'Without it, a session writes only to its own project\'s databases; reading another project\'s connection always asks first.',
            'Production writes still ask every time; the "allow writes for this session" button exists only on dev.',
        ],
    },
    git: {
        label: 'Full git',
        on: 'The sandbox may run any git command on the host - merge, rebase, conflicts, stash, reset…; you confirm each command on a card.',
        warn: [
            'No filter: an allowed command runs exactly as shown on the card - reset --hard, push --force or git config too (which then applies to piperd\'s git and yours). Discarded changes include hidden files the agent does not see. The card marks dangerous commands, but read them.',
            'Still in place: git runs in the verified repository root, hooks do not run, no editor or terminal; the content of hidden files in diffs is not returned to the agent.',
        ],
    },
};
/** A session's START_OPTS: they shape the sandbox, so they change only while it is stopped (applied at Continue) */
function startOptRow(s, k) {
    const t = START_OPT_TEXT[k];
    const on = !!s.opts?.[k];
    const stopped = ['blocked', 'error', 'exited'].includes(s.state);
    return prop(t.label, on ? 'on' : 'off', on ? 'warn' : 'mut', `${t.hint}${on && t.off ? ' - ' + t.off : ''}`, stopped ? h('button', { class: 'mini', onclick: () => (on ? void setStartOpt(s, k, false) : confirmStartOpt(s, k)) }, on ? 'Turn off' : 'Turn on…') : null);
}
async function setStartOpt(s, k, on) {
    try {
        await api.startOpt(s.id, k, on);
        etag = '';
        toast(`${START_OPT_TEXT[k].label} ${on ? 'on' : 'off'} from the next start (${s.title})`);
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
    }
}
function confirmStartOpt(s, k) {
    const t = START_OPT_TEXT[k];
    modal(`${t.label} for ${s.title}?`, h('p', { class: 'warn' }, h('strong', {}, 'Warning. '), t.warn), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', {
        class: 'primary', onclick: () => { closeModal(); void setStartOpt(s, k, true); },
    }, 'Turn on')));
}
/** Effective session locks (label · locked/unlocked · Unlock…/Lock). */
function unlockProps(s) {
    const rows = unlockRows(s.unlock, !!state.gui_flags?.git_hole, state.gui_flags?.unlocks_required !== false).map((r) => {
        const t = UNLOCK_TEXT[r.key];
        return prop(r.label, r.value, r.tone, r.action === 'lock' ? t.on : t.warn[0], h('button', { class: 'mini', onclick: () => (r.action === 'lock' ? void setUnlock(s, r.key, false) : confirmUnlock(s, r.key)) }, r.action === 'lock' ? 'Lock' : 'Unlock…'));
    });
    return rows.length ? [...rows, h('p', { class: 'faint small' }, 'Unlocks apply at once, until locked or the session ends (also after Continue).')] : [];
}
async function setUnlock(s, what, on) {
    try {
        await api.unlock(s.id, what, on);
        etag = '';
        toast(`${UNLOCK_TEXT[what].label}: ${on ? 'unlocked' : 'locked'} (${s.title})`);
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
    }
}
function confirmUnlock(s, what) {
    const t = UNLOCK_TEXT[what];
    modal(`${t.label} for ${s.title}?`, h('div', {}, h('p', { class: 'warn' }, h('strong', {}, 'Warning. '), t.warn[0]), h('p', {}, t.warn[1])), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', {
        class: 'primary', onclick: () => { closeModal(); void setUnlock(s, what, true); },
    }, 'Unlock')));
}
function confirmAllow(s, domain) {
    modal(`Allow ${domain}?`, h('p', {}, `Adds the domain to network.allow in ${s.path}/.hole/settings.json. Applies from the next session start; there you approve the settings change once more in the diff.`), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', {
        class: 'primary', onclick: () => {
            closeModal();
            api.allow(s.id, domain).then((r) => toast(r.data?.written ? `${domain} added - takes effect after a session restart` : `${domain} is already allowed`))
                .catch((e) => toast(String(e instanceof Error ? e.message : e)));
        },
    }, 'Allow')));
}
/** ⌘J: the next item of Needs you (in the queue's order), after the one on screen */
function nextWaiting() {
    const list = queue.needs();
    if (!list.length)
        return toast('Nothing is waiting for you');
    const at = list.findIndex((x) => x.key === selKey || (!selKey && x.session === active));
    openItem(list[(at + 1) % list.length]);
}
/** the file browser is on screen: the Files view, or the grid's Files pane */
function filesShown() {
    return view === 'files' || filesPaneShown();
}
function quickOpen() {
    // from the terminal the file opens in the Files pane; elsewhere in the Files view
    if (view === 'term' && session(active))
        showFilesPane();
    else if (view !== 'files')
        setView('files');
    files.quickOpen();
}
/** F17: the pending card the bar picked ('' = the stack's own choice: the queue's, a red one, the oldest) */
let pendingPick = '';
/** F17: over the terminal one whole card and a bar "N more waiting · ⌘J" - three cards stacked over
 *  the whole height hid the terminal. A session asking for permission in its terminal (Claude's own prompt) while it
 *  is not on screen is one of them ("question"), until closed; "waiting for your input" is the same state and is not */
function renderPending() {
    let box = document.getElementById('pending');
    const picked = pendingPick || (selKey.startsWith('a:p:') ? selKey.slice(4) : '');
    // N3: an off module's card waits in the queue and floats only when you pick its row
    const list = state.pending.filter((p) => { const m = cardModule(p.kind); return !m || on(m) || p.id === picked; });
    const asking = state.sessions.filter((s) => s.state === 'attention' && /permission/i.test(s.detail) && !s.hidden && !onScreen(s.id)
        && !askClosed.has(`${s.id}:${s.detail}`));
    document.body.classList.toggle('pending-open', !!(list.length || asking.length)); // Settings keeps Save/Revert clear of it
    if (!list.length && !asking.length) {
        box?.remove();
        pendingPick = '';
        return;
    }
    if (!box) {
        box = h('div', { id: 'pending' });
        document.body.append(box);
    }
    const shown = shownCard(list, picked, (p) => !!dangerLabel(p));
    const ask = shown ? null : asking.find((s) => `s:${s.id}` === pendingPick) ?? asking[0];
    const rest = [...list.filter((p) => p !== shown).map((p) => KIND_SHORT[p.kind] ?? p.kind), ...asking.filter((s) => s !== ask).map(() => 'question')];
    // rebuilt only when what shows changes: a poll must not swap the button under the mouse
    const ids = [shown ? shown.id + (dangerLabel(shown) ? '!' : '') : `s:${ask.id}:${ask.detail}`, ...rest].join(' ');
    if (box.dataset.ids === ids)
        return;
    box.dataset.ids = ids;
    const hadFocus = box.contains(document.activeElement);
    clear(box);
    box.append(shown ? pendingCard(shown) : askCard(ask));
    if (rest.length) {
        // the bar shows the next one here; ⌘J goes through the whole queue (Needs you)
        const all = [...list.map((p) => p.id), ...asking.map((s) => `s:${s.id}`)];
        const next = all[(all.indexOf(shown ? shown.id : `s:${ask.id}`) + 1) % all.length]; // round the stack, not back and forth
        box.append(h('button', { class: 'pmore', title: 'show the next card here', onclick: () => { pendingPick = next; renderPending(); focusPending(); } }, h('span', { class: 'pdot' }), h('span', { class: 'pmore-n' }, markBidi(moreLine(rest).split(' · ')[0])), h('span', { class: 'pmore-k' }, markBidi(moreLine(rest).split(' · ').slice(1).join(' · '))), h('kbd', {}, kbd('⌘J'))));
    }
    if (hadFocus)
        focusPending(); // a new card arrived while you were on one: y / n keep working
}
/** ⌘J lands on the card (y / n work there, never in the terminal) */
function focusPending() {
    const card = document.querySelector('#pending .pending');
    if (!card)
        return false;
    card.focus({ preventScroll: true });
    return true;
}
function askCard(s) {
    return h('div', { class: 'pending', tabindex: '0' }, h('div', { class: 'phead' }, h('span', { class: 'pdot' }), h('strong', {}, markBidi(sessionName(s))), h('span', { class: 'mut' }, 'question')), s.detail ? h('div', { class: 'pctx' }, markBidi(s.detail)) : null, h('div', { class: 'row pbtns' }, h('button', { onclick: () => { closeAsk(`${s.id}:${s.detail}`); pendingPick = ''; renderPending(); } }, 'Close'), h('button', { class: 'primary', onclick: () => select(s.id) }, 'Open terminal')));
}
/** the red Allow of a card ('' = an ordinary one): production, a foreign host, unmasking, a file the host runs
 *  (code out of the sandbox) - and a settings change that weakens the isolation (piperd marks it) */
function dangerLabel(p) {
    return cardDanger(p);
}
/** the card's text: a diff coloured, ⚠ warnings stand out (textContent only) */
function summaryPre(text) {
    const pre = h('pre', {});
    let inDiff = false;
    for (const line of text.split('\n')) {
        if (line.startsWith('--- ') || line.startsWith('@@'))
            inDiff = true;
        const cls = line.startsWith('⚠') ? 'w' : !inDiff ? '' : line.startsWith('+') && !line.startsWith('+++') ? 'add'
            : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
        pre.append(cls ? h('span', { class: cls }, line) : document.createTextNode(markBidi(line)), '\n');
    }
    return pre;
}
function pendingCard(p) {
    const decide = (ok, always = false, sessionAlways = false) => {
        if (card.dataset.decided)
            return; // one decision, one POST (a held key)
        card.dataset.decided = '1';
        for (const b of card.querySelectorAll('button'))
            b.disabled = true;
        pendingPick = '';
        api.decide(p.id, ok, always, sessionAlways).then(() => { etag = ''; }).catch((e) => {
            etag = '';
            if (decidedElsewhere(e))
                return; // answered meanwhile (another tab, the queue): the next poll removes the card
            delete card.dataset.decided;
            for (const b of card.querySelectorAll('button'))
                b.disabled = false;
            toast('Not decided: ' + String(e instanceof Error ? e.message : e));
        });
    };
    const db = p.kind.startsWith('db_write') && p.payload?.card ? p.payload.card : null;
    const danger = dangerLabel(p);
    const m = cardModel(p, danger);
    // W13 order Deny · Allow, the grant on the right; F17: "always" and "this session" in Allow's ▾ (changes 9.50),
    // the keys on the buttons as chips - never on a red grant (a deliberate click)
    const bs = decisionButtons({ always: p.always, sessionAlways: p.session_always, danger, chips: true });
    const pick = { deny: () => decide(false), always: () => decide(true, true), session: () => decide(true, false, true), allow: () => decide(true) };
    const extra = bs.filter((b) => b.role === 'always' || b.role === 'session');
    const btn = (b) => h('button', { class: b.cls || undefined, title: b.title, onclick: pick[b.role] }, b.label, b.key ? h('kbd', {}, b.key) : null);
    // Allow y ▾: the ▾ its own button (Tab reaches it), drawn as one pill with Allow
    const caret = (b) => h('button', { class: (b.cls || '') + ' pcaret', title: 'more ways to allow', 'aria-haspopup': 'menu', 'aria-label': 'more ways to allow',
        onclick: (e) => {
            const r = e.currentTarget.getBoundingClientRect();
            popMenu(extra.map((x) => ({ label: x.label, run: pick[x.role], ...(x.cls === 'danger' ? { danger: true } : {}), title: x.title })), r.left, r.bottom + 4, 'more ways to allow', () => card.focus());
        } }, h('i', { class: 'fi fi-chev-d', 'aria-hidden': 'true' }));
    const buttons = h('div', { class: 'row pbtns' }, ...bs.filter((b) => !extra.includes(b)).map((b) => b.role === 'allow' && extra.length ? h('span', { class: 'psplit' }, btn(b), caret(b)) : btn(b)));
    const head = h('div', { class: 'phead' }, h('span', { class: 'pdot' }), h('strong', {}, markBidi(p.title)), h('span', { class: 'mut' }, markBidi(m.short)), h('span', { class: 'grow' }), h('span', { class: `prisk ${m.risk.level}` }, m.risk.word));
    const why = m.why ? h('div', { class: 'pwhy' }, 'Asks because ', h('a', { href: '#', title: 'open the rule in Settings', onclick: (e) => {
            e.preventDefault();
            settings.show('security');
            setView('settings');
        } }, markBidi(m.why))) : null;
    const details = h('details', { class: 'pdetails' }, h('summary', {}, 'Details'), summaryPre(p.summary));
    const card = h('div', { class: 'pending' + (danger ? ' pdanger' : '') + (db ? ' dbpending' : ''), tabindex: '0',
        'aria-label': `${p.title}: ${PENDING_KIND[p.kind] ?? p.kind} - y allow, n deny` }, head, db ? dbCard(`${p.title}: ${PENDING_KIND[p.kind] ?? p.kind}`, db, h('span', {}))
        : h('div', { class: 'pbody' }, ...m.warns.map((w) => h('div', { class: 'pwarn' }, markBidi(w))), m.fact ? h('div', { class: 'pfact' }, markBidi(m.fact)) : null, m.context ? h('div', { class: 'pctx' }, markBidi(m.context)) : null), details, why, buttons);
    // y / n only here, with the focus on the card (⌘J) - the terminal's y never approves anything
    card.addEventListener('keydown', (e) => {
        const t = e.target;
        const act = cardKey(e, !!danger, t.matches('input, textarea, select, [contenteditable="true"]'));
        if (!act)
            return;
        e.preventDefault();
        e.stopPropagation();
        pick[act]();
    });
    return card;
}
// ------------------------------------------------------------------ actions / dialogs
function button(label, fn) {
    return h('button', { onclick: fn }, label);
}
async function act(s, a) {
    try {
        const r = (await api.act(s.id, a)).data;
        etag = '';
        // D8: Continue found no saved conversation - it started again with its task, not silently
        if (a === 'retry' && r?.lost)
            toast(`No saved conversation of ${s.title} to continue - it started again with its task`);
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
    }
}
function renameSession(s) {
    const input = h('input', { type: 'text', value: s.title, maxlength: '200', autocomplete: 'off' });
    const save = async () => {
        const t = input.value.trim();
        if (!t || t === s.title) {
            closeModal();
            return;
        }
        try {
            await api.rename(s.id, t);
            etag = '';
            closeModal();
        }
        catch (e) {
            toast(String(e instanceof Error ? e.message : e));
        }
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') {
        e.preventDefault();
        void save();
    } });
    modal('Rename session', input, h('div', { class: 'row' }, button('Cancel', closeModal), h('button', { class: 'primary', onclick: () => void save() }, 'Save')));
    input.focus();
    input.select();
}
function confirmStop(s) {
    modal(`Stop ${s.title}?`, h('p', {}, s.claude_session ? 'The sandbox is destroyed. The conversation can be resumed later with Continue.'
        : 'The sandbox is destroyed. There is no conversation to resume yet: Retry starts the session again.'), h('div', { class: 'row' }, button('Cancel', closeModal), h('button', { class: 'danger', onclick: () => { closeModal(); void act(s, 'stop'); } }, 'Stop')));
}
/** 9.140: the agent a project session's work continues with - the other one; null when it
 *  does not apply (not a project session; a Claude session while the Codex module is off) */
function switchTarget(s, codexOn) {
    if (s.kind !== 'project')
        return null;
    return (s.agent ?? 'claude') === 'claude' ? (codexOn ? 'codex' : null) : 'claude';
}
function agentName(a) {
    return a === 'codex' ? 'Codex' : 'Claude';
}
/** 9.140: always asked - a running session is stopped; `hidden` = the 409 of a conversation that touched files
 *  hidden from agents (R2: the paths, then Hand over anyway; never scrubbed) */
function confirmSwitch(s, hidden = null) {
    const to = switchTarget(s, on('codex'));
    if (!to)
        return;
    const name = agentName(to);
    const go = async (confirmHidden) => {
        closeModal();
        try {
            const r = await api.switchAgent(s.id, confirmHidden);
            etag = '';
            const id = r.data?.session;
            if (id && session(id))
                select(id);
            else if (id)
                pendingSelect = id; // the next poll lists it
        }
        catch (e) {
            const paths = hiddenPaths(e);
            if (paths && !confirmHidden)
                confirmSwitch(s, paths);
            else
                toast(String(e instanceof Error ? e.message : e));
        }
    };
    const live = LIVE.has(s.state) || s.state === 'stopping';
    modal(`Continue with ${name}?`, hidden
        ? h('div', { class: 'resume-hidden', role: 'alertdialog', 'aria-label': 'Conversation holds hidden file content' }, h('p', {}, h('b', {}, 'The conversation touched files hidden from agents. '), `Their content may be in the messages ${name} gets:`), hidden.length ? h('ul', { class: 'st-warn' }, ...hidden.map((p) => h('li', {}, h('code', {}, p))))
            : h('p', { class: 'small' }, 'The isolation rule cannot be read, so the hidden files cannot be named.'))
        : h('p', {}, `${live ? `Stops this ${agentName(s.agent ?? 'claude')} session (its conversation stays resumable) and starts`
            : 'Starts'} ${name} in the same project. ${name} gets the conversation, the worktree it worked in and the git state as a file, and continues from your last request.`), h('div', { class: 'row' }, button('Cancel', closeModal), hidden
        ? h('button', { class: 'danger', onclick: () => void go(true) }, 'Hand over anyway')
        : h('button', { class: 'primary', onclick: () => void go(false) }, `Continue with ${name}`)));
}
/** Update the local cache immediately; a failed preference save must not cause a duplicate start. */
function rememberStarted(result) {
    if (result?.defaults_warning)
        toast(result.defaults_warning);
    if (result?.project_defaults) {
        state.project_session_defaults ??= {};
        state.project_session_defaults[result.project.split('@')[0]] = result.project_defaults;
    }
}
/** false = refused (toasted): the New session dialog stays open with the typed task */
async function start(project, task, title, opts = {}, agent = 'claude', remember = false) {
    try {
        const r = await api.create(project, task, title, opts, agent, remember);
        rememberStarted(r.data);
        etag = '';
        const id = r.data?.id;
        if (id && session(id))
            select(id);
        else if (id)
            pendingSelect = id; // the next poll lists it
        return true;
    }
    catch (e) {
        toast(String(e instanceof Error ? e.message : e));
        return false;
    }
}
/** sessions.START_OPTS in the new-session dialog: what hole gets and why it weakens the sandbox */
const START_OPT_TEXT = {
    docker: {
        label: 'Docker (DinD)', hint: 'hole --with-docker',
        off: 'Turning it off drops the containers and images inside DinD.',
        warn: 'A privileged container with a Docker daemon (rootless inside) runs next to the sandbox - defence in depth, not a hard boundary. Only when the session really needs docker (docker compose, Testcontainers).',
    },
    unrestricted: {
        label: 'Unfiltered network', hint: 'hole -u',
        warn: 'The egress filter will be off: the sandbox reaches anywhere, GitLab and internal services included, and the allowed domains stop applying.',
    },
    idea: {
        label: 'IntelliJ MCP', hint: 'idea tools via host.internal',
        warn: 'IDEA runs on the host and offers execute_terminal_command, run configurations and file access: the session can run code outside the sandbox and read files the hidden-path filter withholds. IDEA must be running with Settings > Tools > MCP Server.',
    },
};
function startOptsBox() {
    const drafts = startOptionsDrafts(state.project_session_defaults ?? {}, state.start_opts_default ?? []);
    let project = '';
    let draft = drafts('');
    const currentAgent = () => on('codex') ? draft.agent : 'claude';
    const enforced = new Set(state.start_opts_enforced ?? []);
    const title = h('span', { class: 'so-project mut small' });
    const sumText = h('span', { class: 'so-sum' });
    // A rebuild armed from ⌘K remains visible and cancellable before starting.
    const rbOff = () => {
        if (rebuildArmed())
            void setRebuild(false).then(syncRb);
        else
            syncRb();
    };
    const rebuildEl = h('span', { class: 'so-rebuild warn hidden' }, '· ' + rebuildSummary(true) + ' ', h('button', { class: 'link small', type: 'button', title: 'the start runs without hole --rebuild', onclick: (e) => {
            e.preventDefault();
            e.stopPropagation();
            rbOff();
        } }, '· cancel'));
    const rbWarn = h('p', { class: 'warn small hidden' }, 'The image is rebuilt for this start: the agent CLI (Claude Code, Codex) and the apt packages are installed again. Several minutes; running sessions keep their image.');
    function syncRb() {
        const armed = rebuildArmed();
        rebuildEl.classList.toggle('hidden', !armed);
        rbWarn.classList.toggle('hidden', !armed);
    }
    const el = h('details', { class: 'startopts' }, h('summary', {}, h('span', { class: 'so-title' }, 'Options'), title, sumText, rebuildEl));
    const body = h('div', { class: 'so-body' });
    el.append(body);
    const summarize = () => {
        const keys = Object.keys(START_OPT_TEXT).filter((k) => draft.opts[k] || enforced.has(k));
        const s = optsSummary(keys, Object.fromEntries(Object.entries(START_OPT_TEXT).map(([k, t]) => [k, t.label])));
        if (!project)
            s.text = 'Pick a project';
        sumText.textContent = markBidi(s.text + (project && s.risky ? ' ⚠' : ''));
        sumText.classList.toggle('warn', !!project && s.risky);
    };
    // F02: the agent is a segment beside the project search, never hidden in Options
    const agentBtns = [];
    const seg = on('codex') ? h('div', { class: 'wk-agent seg', role: 'radiogroup', 'aria-label': 'Agent' }) : null;
    const codexNote = h('p', { class: 'mut small hidden' }, 'Codex keeps its own login in Piper (never the host ~/.codex): the first Codex session asks you to sign in in its terminal (choose "Sign in with Device Code"), later ones reuse it. The conversation view and the digest read Claude transcripts only.');
    const syncAgent = () => agentBtns.forEach((b) => {
        const now = b.dataset.agent === currentAgent();
        b.classList.toggle('on', now);
        b.setAttribute('aria-checked', String(now));
        b.disabled = !project;
        b.title = project ? '' : 'pick a project first';
    });
    for (const [k, label] of [['claude', 'Claude'], ['codex', 'Codex']]) {
        const b = h('button', { type: 'button', role: 'radio', 'data-agent': k, onclick: () => {
                draft.agent = k;
                codexNote.classList.toggle('hidden', currentAgent() !== 'codex');
                syncAgent();
                summarize();
            } }, label);
        agentBtns.push(b);
        seg?.append(b);
    }
    if (on('codex'))
        body.append(codexNote);
    body.append(rbWarn);
    const unavailable = h('p', { class: 'mut small hidden' }, 'Codex is switched off. This session uses Claude; the saved agent stays Codex.');
    body.append(unavailable);
    const optionInputs = new Map();
    for (const k of Object.keys(START_OPT_TEXT)) {
        const t = START_OPT_TEXT[k];
        const cb = h('input', { type: 'checkbox' });
        const warn = h('p', { class: 'warn small hidden' }, t.warn);
        optionInputs.set(k, { cb, warn });
        cb.addEventListener('change', () => {
            if (cb.checked)
                draft.opts[k] = true;
            else
                delete draft.opts[k];
            warn.classList.toggle('hidden', !cb.checked);
            summarize();
        });
        body.append(h('label', { class: 'small', title: t.hint }, cb, ' ', t.label), warn); // N12: the hole flag only in the tooltip
        if (enforced.has(k))
            body.append(h('p', { class: 'mut small' }, 'Enabled globally · ', h('button', { class: 'link small', type: 'button', onclick: () => {
                    closeModal();
                    settings.show(k === 'idea' ? 'sessions' : 'sandbox');
                    setView('settings');
                } }, k === 'idea' ? 'IntelliJ MCP in sessions' : 'Unfiltered network in Sandbox')));
    }
    const remember = h('input', { type: 'checkbox', checked: true });
    remember.addEventListener('change', () => { draft.remember = remember.checked; });
    const note = h('p', { class: 'mut small' });
    body.append(h('label', { class: 'small' }, remember, 'Remember for this project'), note, h('p', { class: 'mut small' }, 'Saved when the session is created. Untick Remember for a one-time choice. Cancel saves nothing.'));
    const load = (key) => {
        project = key;
        const base = key.split('@')[0] ?? '';
        draft = drafts(key);
        title.textContent = markBidi(base);
        title.title = markBidi(base);
        syncAgent();
        for (const [k, { cb, warn }] of optionInputs) {
            cb.checked = !!key && (!!draft.opts[k] || enforced.has(k));
            cb.disabled = !key || enforced.has(k);
            warn.classList.toggle('hidden', !cb.checked);
        }
        remember.checked = draft.remember;
        remember.disabled = !key;
        unavailable.classList.toggle('hidden', !key || on('codex') || draft.agent !== 'codex');
        codexNote.classList.toggle('hidden', !key || currentAgent() !== 'codex');
        const legacy = !state.project_session_defaults?.[base] && !!state.start_opts_default?.length;
        note.textContent = markBidi(key ? 'New sessions in ' + base + ', including its worktrees.' + (legacy ? ' Starts with your previous global defaults.' : '') : 'Pick a project to set its options.');
        summarize();
        syncRb();
    };
    load('');
    return { el, seg, load, value: () => ({ ...draft.opts }), agent: currentAgent, remember: () => draft.remember };
}
const TIPS = [
    ['Needs you comes first', 'The queue puts what waits for you on top: an answer, an approval card, a blocked start.'],
    ['⌘K does everything', 'New session, any view, any project, Setup. Type what you mean.'],
    ['Changes, commit, push', 'The context panel shows the diff of a session; commit and push without leaving the page.'],
];
/** no session and nothing to fix: the empty main area starts the first session and shows the first steps
 * . The tips go once any session exists; "n modules are off" keeps the gallery findable. */
function emptyHub() {
    const projects = state.projects.filter((p) => p.exists);
    const fresh = !state.sessions.some((x) => !x.hidden);
    const off = offModules(state.features);
    const shown = projects.length <= 6 ? projects : projects.slice(0, 5); // "+ 1 more" would only hide one
    const more = projects.length - shown.length;
    return h('div', { class: 'empty hub' }, h('h1', {}, fresh ? 'Start your first session' : 'No session'), h('p', {}, fresh ? 'Pick a project - every git checkout under PROJECT_PATH is here. The session runs in its own sandbox; the first start builds the image and asks for /login once.'
        : on('agent') ? 'Start a new session in a project, or start the Global agent.' : 'Start a new session in a project.'), projects.length ? h('div', { class: 'hub-projects' }, ...shown.map((p) => h('button', { class: 'pill', title: p.path, onclick: () => newSessionDialog(false, undefined, p.key) }, p.title || p.key)), more > 0 ? h('button', { class: 'link small', onclick: () => newSessionDialog() }, `+ ${more} more…`) : null) : null, h('div', { class: 'hub-start' }, h('button', { class: 'primary', onclick: () => newSessionDialog() }, '+ New session'), h('button', { class: 'link', onclick: () => showResume() }, 'Resume a conversation…')), fresh ? h('div', { class: 'hub-tips' }, h('div', { class: 'tlabel-text' }, 'THE FIRST 10 MINUTES'), h('div', { class: 'hub-tip-row' }, ...TIPS.map(([t, d]) => h('div', { class: 'hub-tip' }, h('b', {}, kbd(t)), h('span', { class: 'mut small' }, d))))) : null, h('div', { class: 'hub-links' }, off ? h('button', { class: 'link small', onclick: () => setView('setup') }, `${off} module${off === 1 ? ' is' : 's are'} off - see what they do ›`) : null, h('button', { class: 'link small', onclick: () => setView('setup') }, 'Installation check & features')));
}
/** W15: the Start work dialog on Resume conversation; `initial` = the conversation to select */
function showResume(initial = '') {
    startWork('resume', { initial });
}
/** W15: one dialog for "what do I work on" (IntelliJ's New Project / Get from VCS). F02 (9.82): two tabs on top,
 *  New session ⌥1 · Resume conversation ⌥3; the issue kind is no tab - a pasted issue / MR URL fills the task,
 *  ⌥2 (or the link under the task) opens the issue picker. New ↔ issue is one form (project and task stay);
 *  Resume's pane is built once and keeps its search, its "New conversation…" switches to New with the project. */
function startWork(kind, o = {}) {
    const kinds = workKinds(on('gitlab'), on('github'));
    const form = sessionPane(kind === 'issue', undefined, o.pick ?? '');
    const formEl = h('div', { class: 'wk-pane' }, ...form.nodes);
    const body = h('div', { class: 'wk-body' });
    let resumeEl = null;
    const tabs = kinds.filter((k) => k.id !== 'issue');
    const items = tabs.map((k) => h('button', { class: 'wk-kind', type: 'button', role: 'tab', onclick: () => show(k.id) }, h('span', { class: 'wk-l' }, k.label), h('kbd', { class: 'wk-key' }, kbd(k.id === 'resume' ? '⌥3' : '⌥1'))));
    const forge = on('gitlab') || on('github');
    const hint = forge ? h('span', { class: 'wk-hint faint small' }, 'issue / MR URL → paste it as the task') : null;
    const root = h('div', { class: 'workdlg' }, h('nav', { class: 'wk-tabs', role: 'tablist', 'aria-label': 'Kind of work' }, ...items, hint), body);
    const show = (k, project) => {
        items.forEach((b, i) => {
            const now = tabs[i].id === k || (k === 'issue' && tabs[i].id === 'new');
            b.classList.toggle('on', now);
            b.setAttribute('aria-selected', String(now));
        });
        hint?.classList.toggle('hidden', k === 'resume');
        if (k !== 'resume') {
            body.replaceChildren(formEl);
            if (project)
                form.choose(project);
            form.issue(k === 'issue');
            return;
        }
        if (resumeEl) {
            body.replaceChildren(resumeEl);
            resumeEl.querySelector('input[type="search"]')?.focus();
            return;
        }
        const pane = h('div', { class: 'wk-pane wk-resume' });
        resumeEl = pane;
        body.replaceChildren(pane);
        resumePicker({ modal, closeModal, mount: (r) => pane.append(r),
            newSession: (p) => show('new', p),
            switchTo: (id) => { const s = session(id); const to = s?.claude_session ? switchTarget(s, on('codex')) : null; return to && agentName(to); },
            switchAgent: (id) => { const s = session(id); if (s)
                confirmSwitch(s); },
            opened: (id) => {
                etag = '';
                if (session(id))
                    openSession(id);
                else
                    pendingSelect = id;
            },
        }, o.initial ?? '');
    };
    root.addEventListener('keydown', (e) => {
        const i = kindKey(e);
        const k = i === null ? undefined : ['new', 'issue', 'resume'][i];
        if (!k || !kinds.some((x) => x.id === k))
            return; // ⌥2 only with an issue module
        e.preventDefault();
        e.stopPropagation();
        show(k);
    });
    modal('Start work', root);
    show(kinds.some((k) => k.id === kind) ? kind : 'new');
}
/** W8: the newest ended session linked to issue `iid` of GitLab project `gl` that has a conversation to resume */
function resumableFor(gl, iid) {
    return state.sessions.filter((x) => !x.hidden && x.issue?.gl === gl && x.issue.iid === iid && x.issue.type === 'issue'
        && ['blocked', 'error', 'exited'].includes(x.state) && !!x.claude_session)
        .sort((a, b) => b.created - a.created)[0];
}
/** + Session, ⌘K, the hub: Start work on New session (or From a GitLab issue); the inbox prefill keeps its own
 *  single-pane dialog - the kind is given */
function newSessionDialog(fromIssue = false, prefill, pick = '') {
    if (!prefill)
        return startWork(fromIssue ? 'issue' : 'new', { pick });
    const pane = sessionPane(false, prefill, '');
    modal(`New session: ${prefill.title}`, ...pane.nodes);
    pane.focus();
}
/** the New session form (project, task, where, options): `issue(on)` shows the GitLab issue picker for the chosen project */
function sessionPane(fromIssue, prefill, pick) {
    const projects = state.projects.filter((p) => p.exists);
    const filter = h('input', { type: 'search', placeholder: 'Project…', autocomplete: 'off' });
    const list = h('div', { class: 'plist' });
    const forge = on('gitlab') || on('github');
    const task = h('textarea', { rows: 5, placeholder: forge ? 'Task (optional), or paste an issue / MR URL - typed in after the isolation check'
            : 'Task (optional) - typed in after the isolation check' });
    const issueBox = h('div', { class: 'issues' });
    const shared = h('p', { class: 'warn small hidden wk-shared' });
    const refEl = h('div', { class: 'wk-ref hidden' });
    const root = projectsRoot(projects.map((p) => p.path));
    const opts = startOptsBox();
    const where = whereBox(() => syncShared());
    const running = (key) => state.sessions.filter((x) => x.kind === 'project' && x.project === key && !x.hidden && LIVE.has(x.state));
    let chosen = prefill?.project ?? pick; // a discovered checkout may be missing from the list: it still launches
    let title = prefill?.title ?? '';
    if (prefill)
        task.value = prefill.task;
    const draw = () => {
        const top = list.scrollTop; // a click redraws the list: keep where it was scrolled
        clear(list);
        // W15: a chosen checkout missing from projects.json (a worktree from Resume, an inbox prefill) still shows as chosen
        if (chosen && !projects.some((p) => p.key === chosen))
            list.append(h('button', { class: 'pitem active', type: 'button' }, h('span', { class: 'name' }, chosen), h('span', { class: 'mut' }, chosen.includes('@') ? 'worktree' : 'not in projects.json')));
        for (const p of visible()) {
            // F02: the name and its folder below the projects' root; the full path in the tooltip, "n running" on the right
            const row = projectRow(p.key, p.path, root);
            const n = running(p.key).length;
            list.append(h('button', {
                class: 'pitem' + (p.key === chosen ? ' active' : ''), title: p.path, 'data-project': p.key,
                onclick: () => { chosen = p.key; draw(); if (fromIssue)
                    openIssues(); },
            }, h('span', { class: 'name' }, row.name), h('span', { class: 'mut pdir' }, row.dir), n ? h('span', { class: 'prun small' }, h('span', { class: 'dot st-ready', 'aria-hidden': 'true' }), `${n} running`) : null));
        }
        list.scrollTop = top;
        where.load(chosen);
        opts.load(chosen);
        syncShared();
        // N10-A-3: a filter that matches nothing left an empty list - Start then only said "Pick a project"
        if (projects.length && !visible().length)
            list.append(h('p', { class: 'mut' }, markBidi(`No project matches "${filter.value}".`)));
        if (!projects.length)
            list.append(h('p', { class: 'mut' }, 'projects.json is empty.'), h('button', { onclick: () => void api.importProjects().then((r) => { toast(`Added: ${r.data?.added.length ?? 0}`); etag = ''; closeModal(); }) }, 'Import registered projects'));
    };
    // F02: only while the session would share the main checkout - one line, the whole story in the tooltip
    function syncShared() {
        const sib = chosen ? running(chosen) : [];
        const show = !!sib.length && where.main();
        shared.classList.toggle('hidden', !show);
        shared.textContent = show ? markBidi(`⚠ Shares the working tree with ${sib.length} running ${sib.length === 1 ? 'session' : 'sessions'} - the changes meet.`) : '';
        shared.title = show ? markBidi(`Already running in the project: ${sib.map((x) => x.title).join(', ')}. The new session will work in the same working tree and on the same git branch - give it a separate task (other files/modules), or pick a worktree. Claude is told at the start of the task; you see the changes of both in one Git panel.`) : '';
    }
    // F02: an issue / MR URL as the whole task - the project it belongs to is picked, the chip names it
    let refSeq = 0;
    let refUrl = '';
    let refTitle = false;
    function syncRef() {
        const ref = parseForgeUrl(task.value, on('github'));
        const url = ref ? task.value.trim() : '';
        if (url === refUrl)
            return;
        refUrl = url;
        const mine = ++refSeq;
        refEl.classList.toggle('hidden', !ref);
        if (!ref) {
            refEl.replaceChildren();
            if (refTitle)
                title = '';
            refTitle = false;
            return;
        }
        refTitle = true;
        const found = projectForRef(ref, projects);
        if (found && found !== chosen) {
            chosen = found;
            filter.value = '';
            draw();
        }
        const chip = h('span', { class: 'chip wk-refchip', title: url }, h('b', {}, refChip(ref)));
        refEl.replaceChildren(chip);
        if (!chosen)
            refEl.append(h('span', { class: 'mut small' }, ' pick its project above'));
        title = refChip(ref);
        if (ref.kind !== 'issue' || !chosen)
            return;
        void api.issue(chosen, ref.iid).then((r) => {
            const d = r.data;
            if (mine !== refSeq || !d)
                return;
            if (d.web_url && !url.startsWith(d.web_url))
                return; // the project's tracker has another #N
            title = `${refChip(ref, d.title)}`.slice(0, 200);
            chip.replaceChildren(h('b', {}, refChip(ref)), ' ' + markBidi(d.title));
        }).catch(() => { });
    }
    task.addEventListener('input', syncRef);
    let issuesOf = '';
    // issue picker: search as you type, ↑↓ + Enter, hover/arrow = preview (description + notes), click = pick
    const openIssues = () => {
        issueBox.classList.add('open');
        if (!chosen) {
            issuesOf = '';
            issueBox.replaceChildren(h('p', { class: 'mut small' }, 'Pick a project - its open issues show here.'));
            return;
        }
        if (issuesOf === chosen && issueBox.firstChild)
            return;
        issuesOf = chosen;
        const project = chosen;
        const details = new Map();
        clear(issueBox);
        issueBox.classList.add('open');
        const si = h('input', { type: 'search', placeholder: 'Search issues…', autocomplete: 'off' });
        const rows = h('div', { class: 'plist' });
        const preview = h('div', { class: 'ipreview' }, h('p', { class: 'mut' }, 'Hover an issue (or ↑↓) for a preview, click / Enter to pick.'));
        const note = h('p', { class: 'mut small' });
        issueBox.append(si, h('div', { class: 'isplit' }, rows, preview), note);
        let items = [];
        let glProject;
        let at = -1;
        let picked = -1;
        let shown = 0;
        let seq = 0;
        let typing = 0;
        let hover = 0;
        const mark = () => rows.querySelectorAll('.pitem').forEach((b, i) => {
            b.classList.toggle('active', i === picked);
            b.classList.toggle('cursor', i === at && i !== picked);
        });
        const show = (it) => {
            if (shown === it.iid)
                return;
            shown = it.iid;
            clear(preview);
            preview.append(h('p', { class: 'mut' }, `loading #${it.iid}…`));
            if (!details.has(it.iid))
                details.set(it.iid, api.issue(project, it.iid).then((r) => r.data));
            details.get(it.iid).then((d) => {
                // W9: an issue with a discussion - the picked task targets its latest comment (an edited task stays)
                const last = d?.notes?.length ? d.notes[d.notes.length - 1].id : null;
                if (last && items[picked]?.iid === it.iid && task.value === issueTask(it, glProject))
                    task.value = issueTask(it, glProject, last);
                if (shown !== it.iid || !d)
                    return;
                clear(preview);
                const meta = [d.author && `author ${d.author}`, d.assignees?.length && `assigned to ${d.assignees.join(', ')}`,
                    d.labels?.length && d.labels.join(', '), d.updated_at && `updated ${d.updated_at.slice(0, 10)}`].filter(Boolean).join(' · ');
                preview.append(h('div', { class: 'ihead' }, h('b', {}, `#${d.iid} ${d.title}`), d.web_url ? h('a', { href: d.web_url, target: '_blank', rel: 'noopener' }, d.forge === 'github' ? 'GitHub ↗' : 'GitLab ↗') : null), ...(meta ? [h('p', { class: 'mut small' }, meta)] : []), h('div', { class: 'body' }, (d.description ?? '').trim() || '(no description)'), ...(d.notes ?? []).map((n) => h('div', { class: 'inote' }, h('p', { class: 'mut small' }, `${n.author ?? '?'} · ${(n.created_at ?? '').slice(0, 10)}`), h('div', { class: 'body' }, n.body))));
            }).catch((e) => {
                details.delete(it.iid);
                if (shown !== it.iid)
                    return;
                shown = 0;
                clear(preview);
                preview.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
            });
        };
        const pick = (i) => {
            const it = items[i];
            if (!it)
                return;
            picked = at = i;
            task.value = issueTask(it, glProject);
            title = `#${it.iid} ${it.title}`.slice(0, 200);
            note.textContent = markBidi(`Task prefilled with a link to #${it.iid} - the session loads the description and comments itself. ${kbd('⌘⏎', 'Ctrl')} starts it.`);
            const prev = glProject ? resumableFor(glProject, it.iid) : undefined;
            if (prev) { // W8: the session that already worked on it can go on instead of a new one
                note.append(' ', h('button', {
                    class: 'link', title: `resumes ${prev.title}, compacts its conversation and gives it the task above`,
                    onclick: () => void api.continueSession(prev.id, task.value).then(() => {
                        closeModal();
                        toast(`${prev.title} resumes: the conversation is compacted first, then it gets the task`);
                        etag = '';
                        openSession(prev.id);
                    }).catch((e) => toast(`Could not resume the session: ${String(e instanceof Error ? e.message : e)}`)),
                }, `Resume ${prev.title} instead`));
            }
            mark();
            show(it);
        };
        const render = () => {
            clear(rows);
            items.forEach((it, i) => {
                rows.append(h('button', {
                    class: it.mine?.length ? 'pitem mine' : 'pitem',
                    onclick: () => pick(i),
                    onmouseenter: () => { clearTimeout(hover); hover = window.setTimeout(() => { at = i; mark(); show(it); }, 200); },
                    onmouseleave: () => clearTimeout(hover),
                }, h('span', { class: 'name' }, `#${it.iid} ${it.title}`), ...(it.mine ?? []).map((m) => h('span', { class: 'tag' }, m === 'assigned' ? 'assigned to me' : 'mention')), h('span', { class: 'mut' }, (it.labels ?? []).join(', '))));
            });
            if (!items.length)
                rows.append(h('p', { class: 'mut' }, 'No open issues.'));
            mark();
        };
        const load = async (search) => {
            const mine = ++seq;
            if (!items.length) {
                clear(rows);
                rows.append(h('p', { class: 'mut' }, 'loading issues…'));
            }
            rows.classList.add('busy');
            try {
                const d = (await api.issues(project, search)).data;
                if (mine !== seq)
                    return; // a newer search is under way
                items = d?.issues ?? [];
                glProject = d?.projects?.[0];
                at = picked = -1;
                render();
            }
            catch (e) {
                if (mine !== seq)
                    return;
                clear(rows);
                rows.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
            }
            finally {
                if (mine === seq)
                    rows.classList.remove('busy');
            }
        };
        // N8: an issue picked by a click keeps the focus on its row - "⌘⏎ starts it." must work there too
        rows.addEventListener('keydown', (e) => {
            if (isSend(e, 'doc')) {
                e.preventDefault();
                launch();
            }
        });
        si.addEventListener('input', () => { clearTimeout(typing); typing = window.setTimeout(() => void load(si.value.trim()), 300); });
        si.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                if (!items.length)
                    return;
                at = Math.max(0, Math.min(items.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)));
                mark();
                rows.children[at]?.scrollIntoView({ block: 'nearest' });
                show(items[at]);
            }
            else if (e.key === 'Enter') {
                e.preventDefault();
                if (e.metaKey || e.ctrlKey)
                    return launch();
                pick(at >= 0 ? at : 0);
            }
        });
        void load('');
        si.focus();
    };
    const visible = () => {
        const q = filter.value.toLowerCase();
        return projects.filter((x) => !q || x.key.toLowerCase().includes(q) || x.path.toLowerCase().includes(q));
    };
    let launching = false;
    const launch = () => {
        if (!chosen)
            return toast('Pick a project');
        if (launching)
            return;
        launching = true;
        // S4: the session's checkout - a new worktree is created first, its project key is `<base>@<name>`
        const snapshot = { project: chosen, task: task.value, title, opts: opts.value(), agent: opts.agent(), remember: opts.remember() };
        const go = where.key(snapshot.project).then((key) => prefill
            ? prefill.launch(key, snapshot.task, snapshot.opts, snapshot.agent, snapshot.remember)
            : start(key, snapshot.task, snapshot.title, snapshot.opts, snapshot.agent, snapshot.remember));
        void go.catch((e) => { toast(String(e instanceof Error ? e.message : e)); return false; })
            .then((ok) => { launching = false; if (ok)
            closeModal(); }); // refused: the typed task stays
    };
    filter.addEventListener('input', () => {
        const vis = visible();
        if (!vis.some((p) => p.key === chosen))
            chosen = vis.length === 1 ? vis[0].key : '';
        draw();
    });
    filter.addEventListener('keydown', (e) => {
        const vis = visible();
        const at = vis.findIndex((p) => p.key === chosen);
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            const next = vis[Math.max(0, Math.min(vis.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)))];
            if (next)
                chosen = next.key;
            draw();
            list.querySelector('.pitem.active')?.scrollIntoView({ block: 'nearest' });
            e.preventDefault();
        }
        else if (e.key === 'Enter') {
            e.preventDefault();
            if (!chosen && vis[0]) {
                chosen = vis[0].key;
                draw();
            }
            if (e.metaKey || e.ctrlKey)
                launch();
            else if (fromIssue)
                openIssues();
            else
                task.focus();
        }
    });
    task.addEventListener('keydown', (e) => {
        if (isSend(e, 'doc')) {
            e.preventDefault();
            launch();
        }
    });
    draw();
    if (prefill) {
        task.rows = 12;
        window.setTimeout(() => list.querySelector('.pitem.active')?.scrollIntoView({ block: 'nearest' }), 0);
    }
    // F02: search + agent segment, the projects, the task (a pasted URL becomes a chip), Where, the
    // shared-tree warning, and a foot with the folded Options, Cancel and the light Start ⌘⏎
    const kinds = workKinds(on('gitlab'), on('github'));
    const issueKind = kinds.find((k) => k.id === 'issue');
    const issueLink = issueKind && !prefill ? h('button', { class: 'link small wk-issuelink', type: 'button', title: kbd('⌥2'),
        onclick: () => { fromIssue = !fromIssue; issueBox.classList.toggle('hidden', !fromIssue); if (fromIssue)
            openIssues(); } }, issueKind.label + '…') : null;
    const nodes = [h('div', { class: 'wk-search' }, h('span', { class: 'wk-sico', 'aria-hidden': 'true' }, h('i', { class: 'fi fi-search' })), filter, opts.seg), list,
        prefill ? h('p', { class: 'mut small' }, 'The task is only a link - the session loads the description and comments itself with its GitLab tools. Edit it as needed; the session will be linked to the inbox item.') : null,
        issueLink ? h('div', { class: 'row wk-tasklabel' }, h('span', { class: 'grow' }), issueLink) : null,
        issueBox, task, refEl, where.el, shared,
        h('div', { class: 'row wk-foot' }, opts.el, h('span', { class: 'grow' }), button('Cancel', closeModal), h('button', { class: 'primary wk-start', title: kbd('↑↓ project · ⏎ task · ⌘⏎ start', 'Ctrl'), onclick: launch }, 'Start', h('kbd', {}, kbd('⌘⏎', 'Ctrl'))))];
    if (pick)
        window.setTimeout(() => list.querySelector('.pitem.active')?.scrollIntoView({ block: 'nearest' }), 0);
    const focus = () => {
        if (chosen) { // a prefill, a pick, or the project chosen before a switch
            task.focus();
            task.setSelectionRange(0, 0);
        }
        else
            filter.focus();
    };
    return {
        nodes,
        focus,
        issue(on) {
            fromIssue = on;
            issueBox.classList.toggle('hidden', !on);
            if (on)
                openIssues();
            const si = on && chosen ? issueBox.querySelector('input[type="search"]') : null;
            if (si)
                si.focus();
            else
                focus();
        },
        choose(project) {
            chosen = project;
            filter.value = '';
            draw();
            window.setTimeout(() => list.querySelector('.pitem.active')?.scrollIntoView({ block: 'nearest' }), 0);
        },
    };
}
/** S4: where a new session works - the main checkout, one of the project's worktrees, or a new worktree
 *  (`<project>/.claude/worktrees/<name>`, on a new branch of the same name) */
function whereBox(changed = () => { }) {
    // S4: Main checkout, Existing worktree (its picker), New worktree (name + hint). F02: one segment row, the
    // picker / name below it, the branch a new worktree starts from on the right
    const radio = (v) => h('input', { type: 'radio', name: 'where', value: v });
    const rMain = radio('main');
    const rOld = radio('old');
    const rNew = radio('new');
    const sel = h('select', { 'aria-label': 'the worktree' });
    const name = h('input', { type: 'text', placeholder: 'worktree name', autocomplete: 'off', spellcheck: 'false' });
    const card = (r, label) => h('label', { class: 'wcard' }, r, h('span', {}, label));
    const cMain = card(rMain, 'Main checkout');
    const cOld = card(rOld, 'Existing worktree ▾');
    const cNew = card(rNew, 'New worktree');
    const base = h('span', { class: 'wbase faint small' });
    const newLine = h('span', { class: 'mut small wnew' });
    const el = h('div', { class: 'where hidden' }, h('div', { class: 'wrow' }, h('span', { class: 'wk-label' }, 'Where'), h('div', { class: 'wseg seg' }, cMain, cOld, cNew), base), h('div', { class: 'wextra' }, sel, name, newLine));
    let of = '';
    let info = null;
    let seq = 0;
    const sync = () => {
        const n = name.value.trim();
        for (const [c, r] of [[cMain, rMain], [cOld, rOld], [cNew, rNew]])
            c.classList.toggle('on', r.checked);
        sel.classList.toggle('hidden', !rOld.checked);
        name.classList.toggle('hidden', !rNew.checked);
        const live = info?.worktrees.find((w) => w.main)?.sessions.length ?? 0;
        cMain.title = markBidi(`${info?.current || 'detached'}${live ? ` · ${live} ${live === 1 ? 'session' : 'sessions'} running here` : ''}`);
        cOld.title = rOld.disabled ? 'none yet' : '';
        base.textContent = markBidi(`← ${info?.current || 'HEAD'}`);
        base.title = 'a new worktree branches from here';
        newLine.classList.toggle('hidden', !rNew.checked);
        newLine.textContent = markBidi(rNew.checked && n
            ? (worktreeNameProblem(n, info?.worktrees ?? []) || branchNameProblem(`feat/${n}`, info?.local.map((b) => b.name) ?? [])
                || `${shortDir(info?.root ?? '')}/.claude/worktrees/${n} · new branch feat/${n} from ${info?.current || 'HEAD'}`)
            : 'Name + branch as in "New worktree", then the session starts in it.');
        changed();
    };
    for (const r of [rMain, rOld, rNew])
        r.addEventListener('change', () => { sync(); if (rNew.checked)
            name.focus(); });
    name.addEventListener('input', sync);
    return {
        el,
        load(project) {
            if (project === of)
                return;
            of = project;
            info = null;
            el.classList.add('hidden');
            const mine = ++seq;
            if (!project || project.includes('@'))
                return; // a worktree key is already a place
            void api.gitBranches(project).then((r) => {
                if (mine !== seq || !r.data)
                    return;
                info = r.data;
                clear(sel);
                const wts = info.worktrees.filter((w) => !w.main && w.key);
                sel.append(...wts.map((w) => h('option', { value: w.key }, `${worktreeLabel(w).replace(/^@/, '')} · ${w.branch || 'detached'}`)));
                rOld.disabled = !wts.length;
                cOld.classList.toggle('off', !wts.length);
                rMain.checked = true;
                name.value = '';
                el.classList.remove('hidden');
                sync();
            }).catch(() => { });
            changed();
        },
        /** F02: the session will share the main checkout (no worktree picked) */
        main: () => !info || rMain.checked,
        async key(project) {
            if (project !== of || !info || rMain.checked)
                return project;
            if (rOld.checked)
                return sel.value || project;
            const n = name.value.trim();
            const bad = worktreeNameProblem(n, info.worktrees) || branchNameProblem(`feat/${n}`, info.local.map((b) => b.name));
            if (bad)
                throw new Error(`Worktree: ${bad}`);
            const r = (await api.gitWorktree(project, { action: 'add', name: n, branch: `feat/${n}`, new: true })).data;
            if (!r?.key)
                throw new Error('the worktree was not created');
            etag = '';
            return r.key;
        },
    };
}
// W11: #overlay holds a stack of layers (layers.ts); each layer = div.olayer > .modal, the lower ones inert
const layers = new LayerStack();
function modal(title, ...content) {
    title = showBidi(title); // a dialog title names a file / request / issue of the project: a bidi control is a mark
    mountOverlay(title, false, h('h2', {}, title), ...content.filter(Boolean));
}
/** a dialog over the open one (Git window → Push, History, a review's task): Esc / Cancel goes back to it */
function modalOver(title, ...content) {
    title = showBidi(title);
    mountOverlay(title, true, h('h2', {}, title), ...content.filter(Boolean));
}
function topLayer() {
    return layers.top();
}
/** every dialog (modal(), modalOver(), the palette) opens through here */
function mountOverlay(label, over, ...content) {
    const back = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null;
    const layer = h('div', { class: 'olayer' }, h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': label }, ...content));
    layers.open(layer, back, over)?.remove();
    overlay.append(layer);
    for (const l of layers.all())
        l.inert = l !== layer;
    overlay.classList.add('open');
    overlay.classList.toggle('stacked', layers.depth > 1);
    // nothing behind the dialog takes focus or keys (Tab would reach the terminal)
    for (const c of document.body.children)
        if (c !== overlay && c.id !== 'toasts')
            c.inert = true;
}
// writing into a dialog's text area makes Esc ask first (the second Esc closes): a task or an
// MR description is not lost to an Esc meant for a search field (search boxes stay one Esc). Per layer.
overlay.addEventListener('input', (e) => {
    if (!(e.target instanceof HTMLTextAreaElement))
        return;
    const l = e.target.closest('.olayer');
    if (!l)
        return;
    l.dataset.dirty = '1';
    delete l.dataset.escArmed; // written more after the warning: warn again
});
/** the top dialog goes; the one below (if any) is live again and its opener gets the focus */
function closeModal() {
    const gone = layers.pop();
    if (!gone) {
        closeAllModals();
        return;
    }
    gone.layer.remove();
    const top = layers.top();
    if (!top) {
        closedAll(gone.back);
        return;
    }
    top.inert = false;
    overlay.classList.toggle('stacked', layers.depth > 1);
    if (gone.back?.isConnected && top.contains(gone.back))
        gone.back.focus({ preventScroll: true });
    else
        top.querySelector('button, input, select, textarea, [tabindex]')?.focus({ preventScroll: true });
}
/** leaving: every dialog goes (another session, a file, a link) */
function closeAllModals() {
    const all = layers.clear();
    for (const l of all)
        l.layer.remove();
    closedAll(all[all.length - 1]?.back ?? null);
}
function closedAll(back) {
    overlay.classList.remove('open', 'stacked');
    clear(overlay);
    for (const c of document.body.children)
        c.inert = false;
    if (view !== 'term' && back?.isConnected)
        back.focus({ preventScroll: true });
    else
        terms.get(active)?.focus();
    if (tokenAsked) {
        tokenAsked = false;
        if (!polling) {
            if (token())
                startBackground();
            void poll();
        }
    }
}
/** a short message; click = select session `id`, or an explicit action button. W13: × closes it, the mouse over
 *  it holds it, and it stays in Recent alerts (the bell) */
function toast(text, action, cls = '') {
    let box = document.getElementById('toasts');
    if (!box) {
        box = h('div', { id: 'toasts', role: 'status', 'aria-live': 'polite' });
        document.body.append(box);
    }
    const id = typeof action === 'string' ? action : '';
    const act = typeof action === 'object' ? action : null;
    // N2: a state hint (blocked desktop notifications) is one alert, however many reloads repeat it
    notices.add(text, 'toast', act ?? (id ? { label: 'Open', run: () => select(id) } : undefined), undefined, cls === 'notification-help', id || undefined);
    bellSoon();
    const t = h('div', { class: 'toast' + (cls ? ' ' + cls : ''), onclick: () => { if (id)
            select(id); t.remove(); } }, h('span', {}, text), act ? h('button', { class: 'tact', onclick: (e) => { e.stopPropagation(); t.remove(); act.run(); } }, act.label) : null, h('button', { class: 'tclose', title: 'close', 'aria-label': 'close', onclick: (e) => { e.stopPropagation(); t.remove(); } }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })));
    box.append(t);
    while (box.children.length > 4)
        box.firstElementChild.remove();
    let left = act ? 10000 : 6000;
    const tick = window.setInterval(() => {
        if (!t.isConnected)
            return window.clearInterval(tick);
        if (t.matches(':hover'))
            return;
        if (cls === 'notification-help' && (document.body.classList.contains('pending-open') || overlay.classList.contains('open')))
            return;
        left -= 250;
        if (left <= 0) {
            t.remove();
            window.clearInterval(tick);
        }
    }, 250);
}
/** F28: may Piper show desktop notifications - on / blocked (how to allow) / not asked yet */
function desktopPermission(p = 'Notification' in window ? Notification.permission : 'unsupported') {
    if (p === 'granted')
        return { text: 'Desktop notifications on', cls: 'ok', title: 'shown while Piper is not on screen', empty: 'Desktop notifications are on.', ask: false };
    if (p === 'denied')
        return { text: 'Desktop notifications blocked', cls: 'warn', title: 'allow them in the browser\'s site settings (the lock icon by the address)', empty: 'Desktop notifications are blocked.', ask: false };
    if (p === 'default')
        return { text: 'Desktop notifications off', cls: 'off', title: 'the browser asks once', empty: 'Desktop notifications are off.', ask: true };
    return { text: 'No desktop notifications in this browser', cls: 'off', title: '', empty: '', ask: false };
}
/** W13: the Recent alerts list (IntelliJ's Notifications tool window) under the bell - a list, not a menu; opening marks read */
function openNotices(anchor) {
    closeNotices?.(false);
    const bell = () => anchor?.isConnected ? anchor : actbar.querySelector('button[aria-label="Recent alerts"]');
    const at = bell();
    const list = notices.list();
    const unread = notices.unread;
    const close = (focusBack = true) => {
        notices.dismiss(list.map((n) => n.id));
        pop.remove();
        window.removeEventListener('storage', storage);
        document.removeEventListener('mousedown', outside, true);
        closeNotices = null;
        bellSoon();
        if (focusBack)
            bell()?.focus();
    };
    closeNotices = close;
    const outside = (e) => { if (!pop.contains(e.target) && !bell()?.contains(e.target))
        close(false); };
    const storage = (e) => {
        if (e.key !== 'piper.alerts' && e.key !== null)
            return;
        const current = new Set(notices.list().map((n) => n.id));
        for (const row of pop.querySelectorAll('.nt-item')) {
            if (!current.has(Number(row.dataset.notice)))
                row.remove();
        }
        if (!pop.querySelector('.nt-item'))
            empty();
        bellSoon();
    };
    const empty = () => {
        pop.querySelector('.nt-list')?.replaceWith(h('p', { class: 'mut nt-empty' }, emptyText));
        pop.querySelector('.nt-new')?.remove();
        const button = pop.querySelector('.nt-head button.link');
        if (button)
            button.disabled = true;
    };
    // N2: 24 h like the queue's times (an en-US browser showed "01:46 PM" beside the queue's 13:46)
    const time = (ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    // F28: Needs you (an alert with its action) above Earlier; what happened first, the time under it;
    // the foot says whether desktop notifications may show and how long the list is kept
    const perm = desktopPermission();
    const emptyText = `Nothing new. ${perm.empty}`;
    const row = (n) => h('div', { 'data-notice': String(n.id), class: 'nt-item' + (n.read ? '' : ' unread') + (n.action ? ' act' : '') }, h('span', { class: 'nt-dot', 'aria-hidden': 'true' }), h('span', { class: 'nt-body' }, h('span', { class: 'nt-text' }, markBidi(n.text)), h('span', { class: 'nt-time' }, time(n.ts))), n.action ? h('button', { class: 'tact', onclick: () => { close(); n.action.run(); } }, n.action.label) : null);
    const needs = list.filter((n) => n.action);
    const earlier = list.filter((n) => !n.action);
    const pop = h('div', { id: 'notices', role: 'dialog', 'aria-label': 'Recent alerts', tabindex: '-1' }, h('div', { class: 'nt-head' }, h('b', {}, 'Alerts'), unread ? h('span', { class: 'nt-new' }, `${unread} new`) : null, h('span', { class: 'grow' }), h('button', { class: 'link', disabled: !list.length, onclick: () => {
            notices.clear();
            empty();
            bellSoon();
            pop.focus();
        } }, 'Clear all')), list.length ? h('div', { class: 'nt-list' }, needs.length ? h('div', { class: 'nt-sec' }, 'Needs you') : null, ...needs.map(row), needs.length && earlier.length ? h('div', { class: 'nt-sec' }, 'Earlier') : null, ...earlier.map(row))
        : h('p', { class: 'mut nt-empty' }, emptyText), h('div', { class: 'nt-foot' }, h('span', { class: 'nt-perm ' + perm.cls, title: perm.title }, perm.text), perm.ask ? h('button', { class: 'link', onclick: () => void Notification.requestPermission().then(() => { close(false); openNotices(anchor); }) }, 'Turn on') : null, h('span', { class: 'grow' }), h('span', { class: 'nt-keep', title: `the last ${NOTICE_MAX}, in this browser` }, 'Kept 24 h')));
    pop.addEventListener('keydown', (e) => { if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close();
    } });
    document.body.append(pop);
    const r = at?.getBoundingClientRect();
    const pos = placeMenu(r ? { left: r.right + 6, top: r.bottom - 4, right: r.right + 6, bottom: r.bottom - 4 } : { x: 60, y: 60 }, pop.offsetWidth, pop.offsetHeight, window.innerWidth, window.innerHeight);
    pop.style.left = `${r ? Math.min(r.right + 6, window.innerWidth - pop.offsetWidth - 4) : pos.left}px`;
    pop.style.top = `${pos.top}px`;
    pop.style.height = `${pop.getBoundingClientRect().height}px`;
    window.addEventListener('storage', storage);
    document.addEventListener('mousedown', outside, true);
    notices.markRead();
    bellSoon();
    pop.focus();
}
function banner(text) {
    let b = document.getElementById('banner');
    if (!b) {
        b = h('div', { id: 'banner' });
        document.body.prepend(b);
    }
    b.textContent = markBidi(text); // stays until the next poll that piperd answers
}
function toggleInspector() {
    dispatch({ t: 'context' }); // I7: over the agent drawer it shows the context panel; a global view says why not
}
const MAC = isMac(navigator.platform || navigator.userAgent);
// ⌘ held (Alt off a Mac): the strip's tiles show their ⌘1-9 numbers
const holdKey = MAC ? 'Meta' : 'Alt';
window.addEventListener('keydown', (e) => { if (e.key === holdKey)
    document.body.classList.add('cmd-held'); });
window.addEventListener('keyup', (e) => { if (e.key === holdKey)
    document.body.classList.remove('cmd-held'); });
window.addEventListener('blur', () => document.body.classList.remove('cmd-held'));
/** ⌘-keys that reach Piper while the code editor has the focus */
const CODE_PASS = new Set(['p', 'k', 'b', 'j', 'o', 'e', 'i', ',', '\\', '.', '⇧f', '⇧o', '⇧f12', ...'0123456789']);
function globalKeys(e) {
    if (overlay.classList.contains('open')) {
        // a dialog owns the keyboard: no view switch behind it (⌘⏎ & co. are its own)
        if (e.key !== 'Escape')
            return;
        e.preventDefault();
        const top = topLayer();
        if (top?.dataset.dirty && !top.dataset.escArmed) {
            top.dataset.escArmed = '1';
            toast('The unfinished text will be discarded - Esc once more closes the dialog');
            return;
        }
        closeModal();
        return;
    }
    if (e.altKey && !e.metaKey && e.code === 'F7' && filesShown()) { // IntelliJ's Find Usages
        files.findUsages();
        e.preventDefault();
        return;
    }
    if (view === 'db' && dbView.onKey(e))
        return;
    const inGrid = grid && view === 'term';
    // W26: a Database pane of the grid - its keys (⌘⇧L new console, ⌘S, a console's ⌘E / ⌘↩) while the keyboard is in
    // it; ⌘P stays the search everywhere there (N-G9), the terminal keeps ⌘⇧L = the queue
    if (inGrid && e.key.toLowerCase() !== 'p')
        for (const p of dbPanes.values())
            if (p.el.contains(e.target) && p.view.onKey(e))
                return;
    // W2: ⌥F12 while the host module is on (D10/D15: the key lives with its Keys row), ⌥⇧T / ⌥⇧W in the view; W26: in the
    // grid ⌥⇧T opens a shell into the grid, ⌥⇧W closes the focused host-shell pane's shell (elsewhere it stays the terminal's)
    const hk = hostKey(e, view === 'host' || inGrid);
    if (hk === 'toggle' && !keyOn('⌥F12'))
        return;
    if (hk && hk === 'close' && view !== 'host' && !(paneKind(gridFocus) === 'host' && gridIdsShown().includes(gridFocus)))
        return;
    if (hk && (hk === 'toggle' || hostView.on())) {
        if (hk === 'toggle')
            toggleHost();
        else if (hk === 'new') {
            if (view === 'host')
                void hostView.open();
            else
                newGridShell();
        }
        else if (view === 'host')
            hostView.closeActive();
        else
            hostView.close(gridFocus);
        e.preventDefault();
        return;
    }
    if (!appMod(e, MAC))
        return; // ⌘ (Alt off a Mac): Ctrl+K, Esc etc. belong to the terminal
    // focus in the code editor: only Piper's navigation keys pass; the rest is the editor's
    // (⌘G next match, ⌘L go to line, ⌘F find, ⌘D, ⌘[ ⌘], ⌘S, ⌘⇧L … - files.ts binds them there)
    if (e.target.closest?.('.cm-editor') && !CODE_PASS.has(`${e.shiftKey ? '⇧' : ''}${appKey(e)}`))
        return;
    const list = sessionIds(); // the numbers: one order for the strip and the whole queue (G4)
    const typing = (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)
        && !e.target.classList.contains('xterm-helper-textarea');
    if (e.shiftKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !typing) {
        // the focused queue item, else the active session
        const f = document.activeElement instanceof HTMLElement && queue.el.contains(document.activeElement) ? document.activeElement.closest('[data-sid]')?.dataset.sid : '';
        moveInRail(f || active, e.key === 'ArrowUp' ? -1 : 1);
        e.preventDefault();
    }
    else if (/^[1-9]$/.test(appKey(e))) {
        const id = list[Number(appKey(e)) - 1];
        if (id)
            openSession(id); // the same as a click on its tile / queue row; in the grid it stays (I4)
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'l' && e.shiftKey) {
        focusQueue();
        e.preventDefault();
    }
    else if (e.key === 'i') {
        toggleInspector();
        e.preventDefault();
    }
    else if (e.key === ',') {
        setView('settings');
        e.preventDefault();
    }
    else if (e.key === '.' && keyOn('⌘.')) { // 9.14's key for the Feed: its chat is the drawer now
        toggleDrawer();
        e.preventDefault();
    }
    else if (e.key === 'k') {
        palette();
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'f' && e.shiftKey) {
        setView('files');
        files.searchMode();
        e.preventDefault();
    }
    else if (e.key === 'f') {
        const t = terms.get(active);
        // the file shown (Files, or beside the terminal while it has the focus) finds in itself
        if (filesShown() && files.hasFile() && (view === 'files' || files.el.contains(document.activeElement)))
            files.find();
        else if (view === 'files')
            files.searchMode();
        else if (t && view === 'term')
            findIn(t);
        else
            return; // nothing of ours to search here: the browser's own find
        e.preventDefault();
    }
    else if (e.key === 'l' && filesShown() && files.hasFile()) {
        files.gotoLine();
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'o' && e.shiftKey && filesShown() && files.hasFile()) {
        files.outline();
        e.preventDefault();
    }
    else if (appKey(e) === '[' && filesShown() && (files.back() || !navOn())) {
        e.preventDefault();
    }
    else if (appKey(e) === ']' && filesShown() && (files.forward() || !navOn())) {
        e.preventDefault();
    }
    else if ((appKey(e) === '[' || appKey(e) === ']') && navOn()) { // Files without a jump of its own, or elsewhere: the GUI's back / forward
        if (appKey(e) === '[')
            history.back();
        else
            history.forward();
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'd' && e.shiftKey && filesShown() && files.hasFile()) {
        files.toggleDiff();
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'd' && !e.shiftKey && (view === 'files' || !keyOn('⌘D')) && filesShown() && files.hasFile()) { // in the Files pane ⌘D is the Database pane's key
        files.toggleDiff();
        e.preventDefault();
    }
    else if (e.key === 'p') {
        quickOpen();
        e.preventDefault();
    }
    else if (e.key === 'o' && !e.shiftKey) { // ⌘O: the Files pane next to the active session (D1)
        dispatch({ t: 'pane', pane: FILES_PANE });
        e.preventDefault();
    }
    else if (e.key === 'd' && !e.shiftKey && keyOn('⌘D')) { // ⌘D: the project's Database pane next to it (D1)
        dispatch({ t: 'pane', pane: DB_HERE });
        e.preventDefault();
    }
    else if (e.key === 'F12' && e.shiftKey) { // ⇧⌘F12: IntelliJ's Hide All Tool Windows - again restores them
        dispatch({ t: 'zen' });
        e.preventDefault();
    }
    else if (e.key === 'j') {
        // F17: first onto the floating card (y / n there), from it on through the queue
        if (document.activeElement?.closest('#pending') || !focusPending())
            nextWaiting();
        e.preventDefault();
    }
    else if (e.key === 'b') {
        toggleStrip();
        e.preventDefault();
    }
    else if (e.key.toLowerCase() === 'e' && e.shiftKey && view === 'files') { // ⌘⇧E with the keyboard outside Files (after a tab click)
        files.focusTree();
        e.preventDefault();
    }
    else if (e.key === 'e' && !e.shiftKey && on('composer')) { // 9.44: the Composer module off - ⌘E stays the browser's
        toggleComposer();
        e.preventDefault();
    }
    else if (e.key === 'Enter' && e.shiftKey && view === 'term' && keyOn('⌘G') && gridMembers().length > 1) { // 9.45: zoom
        zoomPane(gridIdsShown().includes(gridFocus) && paneKind(gridFocus) !== 'session' ? gridFocus : active);
        e.preventDefault();
    }
    else if (e.key === 'g' && keyOn('⌘G')) { // A8: the grid module off - ⌘G stays the browser's (find next)
        toggleGrid();
        e.preventDefault();
    }
    else if (appKey(e) === '\\' && keyOn('⌘\\')) {
        toggleSplit();
        e.preventDefault();
    }
    else if (appKey(e) === '0' && keyOn('⌘0')) { // the agent module off: ⌘0 stays the browser's (actual size)
        openAgent();
        e.preventDefault();
    }
}
/** ⌘F: xterm renders to a canvas, the browser's own find cannot see it */
function findIn(t) {
    const input = h('input', { type: 'search', placeholder: 'Find in terminal (⏎ next, ⇧⏎ previous)', autocomplete: 'off' });
    input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter')
            return;
        e.preventDefault();
        const found = e.shiftKey ? t.search.findPrevious(input.value) : t.search.findNext(input.value);
        input.classList.toggle('notfound', !found);
    });
    modal('Find', input);
    input.focus();
}
function hasTerminal(x) { return x.pid !== null && LIVE.has(x.state) && !x.hidden; }
/** the host module (D10): host shells can be grid panes (W26) */
function hostsOn() { return !!state.host_terms?.on; }
/** the live sessions, oldest first (stable: panes must not jump when states change) */
function liveIds() { return state.sessions.filter(hasTerminal).sort((a, b) => a.created - b.created).map((x) => x.id); } // hoisted: syncLayout runs at load
/** the grid's panes (W26: session ids, host shells `host-…`, Database panes `db:n`), the user's pick in its order -
 *  9.45: the whole state of the grid, also while Single shows */
function gridMembers() {
    // Explicitly selected sessions stay on screen when they finish; only new split partners must be live.
    return gridPanes(gridPick ?? migratePick(null, lay.center.kind === 'grid', liveIds()), state.sessions.filter((x) => !x.hidden).map((x) => x.id), hostsOn() ? hostItems(state).map((x) => x.id) : null, on('db'), on('http'));
}
/** 9.45: the pane zoomed alone (⤢ on a Files / Database / HTTP / host-shell pane), '' = none */
function zoomed() {
    const c = lay.center;
    return c.kind === 'single' && c.pane && gridMembers().includes(c.pane) ? c.pane : '';
}
/** the pane ids shown now, in order: the grid's panes (9.45: all of them - a preset never hides one), a zoomed pane */
function gridIdsShown() {
    if (!grid)
        return [];
    const z = zoomed();
    if (z)
        return [z];
    const ids = gridMembers();
    return besideOnly(ids) ? [...ids.filter((id) => paneKind(id) === 'session'), ...ids.filter((id) => paneKind(id) !== 'session')] : ids;
}
/** the sessions among the shown panes */
function gridSessions() {
    return gridIdsShown().map((id) => session(id)).filter((x) => !!x);
}
function setGridLayout(id) {
    gridLayout = id;
    writeStore('piper.gridlayout', id);
    if (grid)
        refreshGrid();
}
/** A shape that keeps the pane controls reachable at the workspace's current size. */
function effectiveGridLayout(n, id = gridLayout) {
    // A Beside pair stays side by side; explicit grids with several tools respect their preset.
    if (n === 2 && !gridAdd && !zoomed() && besideOnly(gridMembers()))
        return 'one-rest';
    if (gridAdd && gridMembers().length === 1)
        return 'columns';
    return layoutFor(id, n, bodyEl.clientWidth || Infinity, bodyEl.clientHeight || Infinity);
}
/** redraw the grid's panes with the terminals frozen (one fit each at the end) */
function refreshGrid() {
    for (const t of terms.values())
        t.freeze(true);
    renderMain();
    window.setTimeout(() => { for (const t of terms.values())
        t.freeze(false); }, 200);
}
/** W3: the layout picker under the Grid's layout button */
let layoutMenuEl = null;
let closeBeside = () => undefined;
/** F03: Beside ▾ - what can go beside the terminal (a tick = it is there; a click puts it there or takes it away),
 *  then the grid's Layout presets with their thumbnails (9.45) and Choose panes… */
function besideMenu(anchor, s, live) {
    layoutMenuEl?.remove();
    const proj = s.kind === 'project';
    const open = grid && !zoomed() && view === 'term';
    const n = gridMembers().length;
    const rest = open ? n : restoreMembers(layoutCtx()).length; // 9.45's "Grid n": what ⌘G brings back
    const full = editMembers(lay, layoutCtx()).length >= GRID_MAX;
    const row = (icon, label, key, onNow, why, title, run) => h('button', { role: 'menuitemcheckbox', class: 'bitem', 'aria-checked': String(onNow), disabled: !!why, title: why || title,
        onclick: () => { close(); run(); } }, h('span', { class: 'bcheck', 'aria-hidden': 'true' }, onNow ? '✓' : ''), h('i', { class: `fi fi-${icon}`, 'aria-hidden': 'true' }), h('span', { class: 'grow' }, label), key ? h('kbd', {}, kbd(key)) : null);
    const items = [
        proj ? row('dir', 'Files', '⌘O', paneShown(FILES_PANE), '', 'switch the beside tool to Files; use Choose panes for multiple tools', () => dispatch({ t: 'pane', pane: FILES_PANE })) : null,
        proj && on('db') ? row('db', 'Database', '⌘D', paneShown(DB_HERE), '', 'switch the beside tool to this project\'s databases; use Choose panes for multiple tools', () => dispatch({ t: 'pane', pane: DB_HERE })) : null,
        proj && on('http') ? row('http', 'HTTP', '', paneShown(HTTP_PANE), '', 'switch the beside tool to HTTP; use Choose panes for multiple tools', () => dispatch({ t: 'pane', pane: HTTP_PANE })) : null,
        hostsOn() ? row('term', 'Host shell', '⌥⇧T', false, full ? `${GRID_MAX} panes at most` : '', 'a login shell on this Mac beside the terminal - no sandbox', () => newGridShell(anchor)) : null,
        on('grid') ? row('grid', open ? 'Another session…' : `Another session… / Grid${rest > 1 ? ` ${rest}` : ''}`, open ? '' : '⌘G', false, full ? `${GRID_MAX} panes at most` : '', 'a session, host shell, Files or Database pane in the grid', () => { if (open)
            dispatch({ t: 'addCell' });
        else
            toggleGrid(true); }) : null,
        on('composer') && !hiddenTabs.has('composer') ? row('pen', 'Composer', '⌘E', composer.open(), live && (view === 'term' || view === 'convo') ? '' : 'only for a running session in Terminal or Conversation', 'multi-line input with templates', () => toggleComposer()) : null,
    ].filter((x) => !!x);
    const thumb = (id) => {
        const p = presetOf(id);
        const sh = shapeOf(id, id === 'auto' ? Math.max(1, Math.min(n || 2, GRID_MAX)) : Math.min(p.cap, Math.max(n, 2)));
        return h('span', { class: `lthumb ${sh.dir}` }, ...sh.groups.map((k) => h('span', { class: 'lgroup' }, ...Array.from({ length: k }, () => h('span', { class: 'lcell' })))));
    };
    // 9.135: Layout only where it changes the screen - the open grid with 2+ panes, one card per distinct shape
    const choices = on('grid') && open ? layoutChoices(n, effectiveGridLayout(n), (id) => effectiveGridLayout(n, id) === id) : [];
    const box = h('div', { class: 'layoutmenu besidemenu', role: 'menu', 'aria-label': 'beside the terminal' }, ...items, choices.length ? h('div', { class: 'sec-h lsec' }, 'Layout', h('span', { class: 'mut small' }, ` · ${n} panes in grid`)) : null, choices.length ? h('div', { class: 'lgrid' }, ...choices.map((c) => h('button', {
        role: 'menuitemradio', class: 'lcard' + (c.on ? ' on' : ''), 'aria-checked': String(c.on), disabled: c.disabled,
        title: c.disabled ? 'needs more space - a wider window or fewer panes' : presetOf(c.id).label,
        onclick: () => { close(); setGridLayout(c.id); },
    }, thumb(c.id), h('span', { class: 'llabel' }, presetOf(c.id).label)))) : null, open ? h('div', { class: 'lfoot' }, h('button', { class: 'ghost', onclick: () => { close(); gridChooser(); } }, 'Choose panes…'), h('span', { class: 'grow' }), h('button', { class: 'ghost', onclick: () => { close(); toggleGrid(false); } }, kbd('Single ⌘G'))) : null);
    const close = () => { box.remove(); layoutMenuEl = null; closeBeside = () => undefined; document.removeEventListener('mousedown', off, true); anchor.setAttribute('aria-expanded', 'false'); };
    closeBeside = close;
    const off = (ev) => { if (!(ev.target instanceof Node && (box.contains(ev.target) || anchor.contains(ev.target))))
        close(); };
    box.addEventListener('keydown', (e) => {
        const bs = [...box.querySelectorAll('button:not(:disabled)')];
        const i = bs.indexOf(document.activeElement);
        const d = { ArrowDown: 1, ArrowUp: -1, ArrowRight: 1, ArrowLeft: -1 }[e.key];
        if (d)
            bs[Math.max(0, Math.min(bs.length - 1, i + d))]?.focus();
        else if (e.key === 'Escape') {
            close();
            anchor.focus();
        }
        else
            return;
        e.preventDefault();
        e.stopPropagation();
    });
    document.body.append(box);
    const r = anchor.getBoundingClientRect();
    const w = box.getBoundingClientRect().width;
    box.style.left = `${Math.max(4, Math.min(r.right - w, window.innerWidth - w - 4))}px`;
    box.style.top = `${r.bottom + 4}px`;
    layoutMenuEl = box;
    anchor.setAttribute('aria-expanded', 'true');
    box.querySelector('button:not(:disabled)')?.focus();
    document.addEventListener('mousedown', off, true);
}
function loadGridPick() {
    try {
        const v = JSON.parse(window.localStorage.getItem('piper.grid') ?? 'null');
        return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 3 * GRID_MAX) : null; // + panes kept while their module is off
    }
    catch {
        return null;
    }
}
/** the grid's panes are `ids` from now on, remembered in this browser */
function storePick(ids) {
    gridPick = keepPick(ids, gridPick, hostsOn(), on('db'), on('http'));
    try {
        window.localStorage.setItem('piper.grid', JSON.stringify(gridPick));
    }
    catch {
        /* private window: the pick lasts until reload */
    }
}
/** change the grid's panes (starting from what is shown now), remember it in this browser */
function gridEdit(fn) {
    storePick(fn(gridMembers()));
    if (grid) {
        for (const t of terms.values())
            t.freeze(true);
        renderMain();
        window.setTimeout(() => { for (const t of terms.values())
            t.freeze(false); }, 200);
    }
}
/** a grid pane's title: what it shows (session / HOST strip / Database connection), ◀ ▶ (swap with the neighbour),
 *  ⋯ (open it as its tab / view), ⤢ (9.45: that pane alone - Single; ⤡ back to the grid), ✕ (out of the grid only -
 *  a shell keeps running) */
function paneLabel(el, id) {
    clear(el);
    const ids = gridIdsShown();
    const at = ids.indexOf(id);
    const z = zoomed() === id;
    const fixed = besideOnly(ids);
    const canSwap = (from, to) => !fixed || (paneKind(from) !== 'session' && paneKind(to) !== 'session');
    const canMove = (d) => at + d >= 0 && at + d < ids.length && canSwap(id, ids[at + d]);
    const move = (d) => { if (canMove(d))
        gridEdit((l) => swapPanes(l, id, ids[at + d])); };
    // 9.45: drag the title onto another pane = the two swap places
    el.draggable = !z && ids.some((other) => other !== id && canSwap(id, other));
    el.ondragstart = (e) => { e.dataTransfer?.setData('application/x-piper-pane', id); if (e.dataTransfer)
        e.dataTransfer.effectAllowed = 'move'; };
    const pane = el.parentElement;
    if (pane) {
        pane.ondragover = (e) => { if ((!fixed || paneKind(id) !== 'session') && e.dataTransfer?.types.includes('application/x-piper-pane')) {
            e.preventDefault();
            pane.classList.add('droptarget');
        } };
        pane.ondragleave = () => pane.classList.remove('droptarget');
        pane.ondrop = (e) => {
            pane.classList.remove('droptarget');
            const from = e.dataTransfer?.getData('application/x-piper-pane');
            if (!from)
                return;
            e.preventDefault();
            if (from !== id && canSwap(from, id))
                gridEdit((l) => swapPanes(l, from, id));
        };
    }
    const kind = paneKind(id);
    let alone;
    if (kind === 'host') {
        const x = hostItems(state).find((i) => i.id === id);
        const ex = x ? exitText(x) : '';
        el.append(h('span', { class: 'ht-pill' + (ex ? ' dead' : '') }, ex ? 'HOST' : 'HOST · no sandbox'), h('span', { class: 'name' }, x?.title ?? 'Host shell'), ex ? h('span', { class: 'ht-rc' }, ex) : h('span', { class: 'mut small mono', title: markBidi(x?.path ?? '') }, markBidi(tildePath(x?.path ?? '', state.host_terms?.user ?? ''))));
        if (ex && x) {
            const again = h('button', { class: 'mini', title: `a new host shell in ${x.project}, in this pane`,
                onclick: () => void hostView.open(x.project, again, (nid) => gridEdit((l) => l.map((i) => (i === id ? nid : i)))) }, 'New shell here');
            el.append(again, h('button', { class: 'mini', title: 'close the ended shell', onclick: () => hostView.close(id) }, 'Close'));
        }
        alone = ['this shell in the Host view (⌥F12)', () => { hostView.pick(id); setView('host'); }];
    }
    else if (kind === 'files') {
        const p = files.project();
        el.append(h('span', { class: 'name' }, 'Files'), h('span', { class: 'mut small' }, p ? `${p} · follows the active session` : 'follows the active session'));
        alone = ['the whole Files tab', () => setView('files')];
    }
    else if (kind === 'http') { // 9.42
        const p = httpPanel.project();
        el.append(h('span', { class: 'name' }, 'HTTP'), h('span', { class: 'mut small' }, p ? `${p} · follows the active session` : 'follows the active session'));
        alone = ['the whole HTTP tab', () => setView('http')];
    }
    else if (kind === 'db') {
        const c = dbPanes.get(id)?.view.current();
        const here = id === DB_HERE ? ' · follows the active session' : '';
        if (c)
            el.append(classBadge(c.cls), h('span', { class: 'name' }, c.label), h('span', { class: 'mut small' }, c.project + here));
        else
            el.append(h('span', { class: 'name' }, 'Database'), h('span', { class: 'mut small' }, here ? 'the active session\'s project - pick a connection below' : 'no connection yet - pick one below'));
        alone = ['this connection in the Database view', () => openDb(c?.name ?? '')];
    }
    else {
        const s = session(id);
        // F14: the bar is the state (dot) and the name; the words in its tooltip
        if (s)
            el.append(dot(s), h('span', { class: 'name', title: `${LABEL[s.state] ?? s.state}${s.state === 'waiting' || s.state === 'attention' ? ' · ' + ago(s.since || s.created) : ''}` }, s.kind === 'advisor' ? 'Global agent' : s.title));
        if (s && ['exited', 'error', 'blocked'].includes(s.state))
            el.append(h('button', { class: 'mini primary', onclick: () => void act(s, 'retry') }, continueLabel(s)));
        alone = ['the session\'s Conversation tab', () => { select(id); setView('convo'); }];
    }
    const more = h('button', { class: 'mini', title: 'more', 'aria-label': 'pane menu', onclick: () => {
            const r = more.getBoundingClientRect();
            // F14: ◀ ▶ moved here from the bar (dragging the title swaps too)
            const why = (d) => canMove(d) ? false : fixed && at + d >= 0 && at + d < ids.length ? 'the agent stays on the left, project tools on the right' : 'no pane there';
            popMenu([{ label: `Open ${alone[0]}`, run: alone[1] }, { label: z ? 'Back to the grid' : 'Alone (Single)', run: () => zoomPane(id), keys: kbd('⇧⌘⏎') },
                z ? false : { label: 'Move left', title: 'swap with the pane before (or drag the title)', disabled: why(-1), run: () => move(-1) },
                z ? false : { label: 'Move right', title: 'swap with the pane after (or drag the title)', disabled: why(1), run: () => move(1) },
                null, { label: 'Remove from grid', run: () => dispatch({ t: 'remove', id }) }], r.left - 160, r.bottom + 4, 'pane menu', () => more.focus());
        } }, fi('more', 'gi'));
    el.append(h('span', { class: 'pane-tools' }, h('button', { class: 'mini', 'data-act': z ? 'unzoom' : 'zoom', title: kbd(z ? 'back to the grid (⇧⌘⏎)' : 'this pane alone - the grid keeps its panes (⇧⌘⏎)'),
        'aria-label': z ? 'back to the grid' : 'this pane alone', onclick: () => zoomPane(id) }, fi(z ? 'shrink' : 'expand', 'gi')), more, h('button', { class: 'mini', 'data-act': 'remove', title: kind === 'host' ? 'remove from grid (the shell keeps running)' : 'remove from grid', 'aria-label': 'remove from grid',
        onclick: () => dispatch({ t: 'remove', id }) }, fi('x', 'gi'))));
}
/** 9.45: ⤢ / ⇧⌘⏎ - the pane alone (a session becomes the active one, Single); zoomed already = back to the grid */
function zoomPane(id) {
    if (zoomed() || (!grid && view === 'term' && gridMembers().length > 1 && lay.center.kind === 'single')) {
        dispatch({ t: 'zoom', pane: '' });
        return;
    }
    if (paneKind(id) === 'session') {
        if (id !== active)
            makeActive(id);
        dispatch({ t: 'single' });
        return;
    }
    dispatch({ t: 'zoom', pane: id });
}
/** W26: the Database panes of the grid, each its own DbView (own connection, `piper.db.grid-n`); made when first
 *  shown, kept while the page lives (a view has no dispose - its timers run only while it is visible). D1: `db:here`
 *  lists only the active session's project (what the DB side pane was, `piper.db.side`) */
const dbPanes = new Map();
function dbPane(id) {
    let p = dbPanes.get(id);
    if (!p) {
        const dv = createDbView({
            toast, modal, closeModal,
            projects: () => state.projects.filter((x) => x.exists).map((x) => ({ key: x.key, title: x.title || x.key })),
            pending: () => state.pending,
            storeKey: id === DB_HERE ? 'side' : `grid-${id.slice(3)}`, compact: true,
            ...(id === DB_HERE ? { scope: () => { const s = session(active); return s?.kind === 'project' ? s.project : ''; } } : {}),
            toAgent: (text) => toSession(text),
            // "→ <session>": the active session's composer gets it; none = no button
            agentLabel: () => { const s = session(active); return s && hasTerminal(s) ? (s.kind === 'advisor' ? 'Global agent' : s.title) : ''; },
        });
        dv.el.tabIndex = -1; // a click into the pane takes the keyboard there (its ⌘⇧L, ⌘S)
        const label = h('div', { class: 'tlabel' });
        const el = h('div', { class: 'term dbpane hidden' }, label, dv.el);
        // a connection picked / switched in the pane: its title now, not at the next poll
        dv.el.addEventListener('click', () => window.setTimeout(() => { if (grid && view === 'term')
            paneLabel(label, id); }, 400));
        dv.show();
        p = { view: dv, el, label };
        dbPanes.set(id, p);
    }
    return p;
}
/** W26: the keyboard went into a grid pane - it is the focused one (a session pane also becomes the active one) */
const gridFocusWired = new WeakSet();
function wireGridFocus(el, id) {
    if (gridFocusWired.has(el))
        return;
    gridFocusWired.add(el);
    el.addEventListener('focusin', () => {
        if (!grid || view !== 'term' || gridFocus === id)
            return;
        gridFocus = id; // a session pane: its onFocus also makes it the active one
        for (const p of gridIdsShown())
            gridEl(p)?.classList.toggle('focused', p === id);
    });
}
/** the element of a shown grid pane */
function gridEl(id) {
    const k = paneKind(id);
    const s = k === 'session' ? session(id) : undefined;
    return k === 'session' ? s && hasPty(s) ? terms.get(id)?.el : sessionPanels.get(id) : k === 'db' ? dbPanes.get(id)?.el : k === 'files' ? filesPaneEl : k === 'http' ? httpPaneEl : hostView.term(id)?.el;
}
/** the project of the focused grid pane (⌥⇧T opens the shell there), else the active session's */
function gridProject() {
    const k = paneKind(gridFocus);
    if (gridFocus && k === 'host')
        return hostItems(state).find((x) => x.id === gridFocus)?.project ?? '';
    if (gridFocus && k === 'db')
        return dbPanes.get(gridFocus)?.view.current()?.key ?? '';
    if (gridFocus && k === 'files')
        return files.project();
    if (gridFocus && k === 'http')
        return httpPanel.project();
    const s = session(gridFocus || active);
    return s?.kind === 'project' ? s.project : '';
}
/** ⌥⇧T in the grid / Add to grid: a new host shell into the grid (W26 decision); a full grid keeps it in the Host view */
function newGridShell(at) {
    if (!hostsOn()) {
        openHostShell('');
        return;
    }
    void hostView.open(gridProject(), at ?? slotEl, (nid) => {
        if (editMembers(lay, layoutCtx()).length >= GRID_MAX) {
            toast(`The grid has ${GRID_MAX} panes - the new shell is in the Host view (⌥F12)`);
            hostView.pick(nid);
            setView('host');
            return;
        }
        dispatch({ t: 'add', id: nid });
        window.setTimeout(() => hostView.term(nid)?.focus(), 50);
    });
}
/** a Database pane into the grid (the lowest free db:n) */
function addDbPane() {
    const id = nextDbPane(editMembers(lay, layoutCtx()));
    if (!id) {
        toast(`The grid has ${GRID_DB_MAX} Database panes at most`);
        return;
    }
    dispatch({ t: 'add', id }); // a full grid: the reducer says so
}
const slotEl = h('div', { class: 'term slot' });
let gridCells = [];
const splitEls = new Map();
let gridResizeFrame = 0;
new ResizeObserver(() => {
    if (!grid || view !== 'term' || gridResizeFrame)
        return;
    gridResizeFrame = requestAnimationFrame(() => { gridResizeFrame = 0; if (grid && view === 'term')
        renderMain(); });
}).observe(bodyEl);
/** the splits per shape (W3: piper.gridfr2 {shapeKey: GridFr}); the old piper.gridfr seeds the shapes it had */
function loadGridFr(sh) {
    try {
        const all = JSON.parse(readStore('piper.gridfr2') ?? '{}');
        return fitFr(sh, all[shapeKey(sh)] ?? legacyFr(sh, JSON.parse(readStore('piper.gridfr') ?? 'null')));
    }
    catch {
        return fitFr(sh, null);
    }
}
function saveGridFr() {
    try {
        const all = JSON.parse(readStore('piper.gridfr2') ?? '{}');
        all[shapeKey(gridShape)] = gridFr;
        writeStore('piper.gridfr2', JSON.stringify(all));
    }
    catch {
        writeStore('piper.gridfr2', JSON.stringify({ [shapeKey(gridShape)]: gridFr }));
    }
}
/** place the panes and the splitters from gridFr (no DOM moves, only styles) */
function layoutGrid() {
    const sh = shapeOf(effectiveGridLayout(gridCells.length), gridCells.length); // a layout never hides a pane
    if (shapeKey(sh) !== shapeKey(gridShape)) {
        gridShape = sh;
        gridFr = loadGridFr(sh);
    }
    const rects = paneRects(gridShape, gridFr);
    gridCells.forEach((c, i) => {
        const r = rects[i];
        c.classList.add('gcell');
        Object.assign(c.style, { left: r.left, top: r.top, width: r.width, height: r.height });
    });
    const bars = splitBars(gridShape, gridFr);
    const want = new Set(bars.map((b) => b.key));
    for (const [k, el] of splitEls)
        if (!want.has(k)) {
            el.remove();
            splitEls.delete(k);
        }
    for (const b of bars) {
        let el = splitEls.get(b.key);
        if (!el) {
            el = splitter(b);
            splitEls.set(b.key, el);
        }
        if (!el.isConnected)
            bodyEl.append(el);
        Object.assign(el.style, b.rect);
    }
}
/** W26: host-shell / Database panes the grid does not show now are hidden (a host shell's terminal back in the Host
 *  view is left alone there) */
function hideGridExtras(shown) {
    for (const [id, p] of dbPanes)
        if (!shown.includes(id))
            p.el.classList.add('hidden');
    if (!shown.includes(FILES_PANE))
        filesPaneEl.classList.add('hidden');
    if (!shown.includes(HTTP_PANE))
        httpPaneEl.classList.add('hidden');
    for (const x of hostItems(state)) {
        if (shown.includes(x.id))
            continue;
        const el = hostView.term(x.id, false)?.el;
        if (el?.parentElement === bodyEl)
            el.classList.add('hidden');
    }
}
function clearGridLayout() {
    // Removed/replaced panes stay mounted but are no longer in gridCells. Clear their
    // old dimensions too, or reopening an idle session in Single keeps its grid width.
    for (const c of bodyEl.querySelectorAll(':scope > .gcell')) {
        c.classList.remove('gcell');
        Object.assign(c.style, { left: '', top: '', width: '', height: '' });
    }
    gridCells = [];
    gridShape = { dir: 'col', groups: [] };
    for (const el of splitEls.values())
        el.remove();
    splitEls.clear();
}
/** a splitter in the gap: drag to resize (terminals frozen while dragging), double-click = halfway between its neighbours */
function splitter(b) {
    const { group, index, bar } = b;
    const el = h('div', { class: `gsplit ${bar}`, title: 'drag to resize, double-click = halfway between its neighbours' });
    el.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        el.classList.add('drag');
        for (const t of terms.values())
            t.freeze(true);
        const box = bodyEl.getBoundingClientRect();
        const move = (ev) => {
            gridFr = dragSplit(gridFr, group, index, bar === 'v' ? (ev.clientX - box.left) / box.width : (ev.clientY - box.top) / box.height);
            layoutGrid();
        };
        const up = () => {
            el.classList.remove('drag');
            el.removeEventListener('pointermove', move);
            el.removeEventListener('pointerup', up);
            el.removeEventListener('pointercancel', up);
            saveGridFr();
            for (const t of terms.values())
                t.freeze(false); // one fit (one SIGWINCH) per pane, at the end
        };
        el.addEventListener('pointermove', move);
        el.addEventListener('pointerup', up);
        el.addEventListener('pointercancel', up);
    });
    el.addEventListener('dblclick', () => {
        gridFr = resetSplit(gridFr, group, index);
        layoutGrid();
        saveGridFr();
    });
    return el;
}
/** the empty grid cell "Add to grid" (W26): a live session, a host shell (an open one or a new one) or a Database pane */
/** 9.38, 9.45: the grid is just its panes - the "Add to grid" cell only when asked for: "+ Add
 *  pane", or the Grid click with fewer than 2 panes (fx.add); adding a pane or × takes it away */
function gridSlot(shown) {
    if (!gridAdd || !on('grid'))
        return null; // 9.37: without the grid module only the beside pair - nothing to add
    const members = gridMembers();
    if (members.length >= GRID_MAX || zoomed() || shown >= GRID_MAX)
        return null;
    clear(slotEl);
    // F14: a thin "+ Add pane", the choice in a popover; the count only near the limit
    const add = h('button', { class: 'ghost slot-add', 'aria-haspopup': 'menu', onclick: () => addPaneMenu(add) }, '+ Add pane');
    slotEl.append(add);
    if (members.length >= GRID_MAX - 2)
        slotEl.append(h('span', { class: 'mut small slot-n' }, `${members.length} of ${GRID_MAX} panes`));
    slotEl.append(h('button', { class: 'icon ghost slot-x', title: 'cancel adding a pane', 'aria-label': 'close', onclick: cancelGridAdd }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })));
    slotEl.style.order = '99';
    if (slotFresh) { // asked for just now (Beside ▾, Grid): the popover opens at once
        slotFresh = false;
        window.setTimeout(() => { if (add.isConnected)
            addPaneMenu(add); }, 0);
    }
    return slotEl;
}
let slotFresh = false;
/** F14: what "+ Add pane" offers - one list as in Beside ▾: sessions, host shells, the active session's Files /
 *  HTTP / Database, more Database panes; typing filters */
function addPaneEntries() {
    const members = gridMembers();
    const rest = state.sessions.filter((x) => hasTerminal(x) && !members.includes(x.id));
    const hosts = hostsOn() ? hostItems(state).filter((x) => x.alive && !members.includes(x.id)) : null; // D5: an ended one is no pane to add
    const dbs = members.filter((x) => paneKind(x) === 'db' && x !== DB_HERE).length;
    const cur = session(active);
    const proj = cur?.kind === 'project';
    const pname = proj ? projectShort(cur) : '';
    const ic = (name) => h('i', { class: `fi fi-${name}`, 'aria-hidden': 'true' }); // 9.120: line icons, as Beside ▾
    const shell = () => ic('term');
    const gp = gridProject();
    return [
        { group: 'Sessions' },
        ...rest.map((x) => ({ id: x.id, label: h('span', { class: 'ap-row' }, dot(x), ' ', markBidi(x.kind === 'advisor' ? 'Global agent' : x.title), h('span', { class: 'mut' }, ` — ${LABEL[x.state] ?? x.state}`)), search: x.kind === 'advisor' ? 'Global agent' : x.title, run: () => dispatch({ t: 'add', id: x.id }) })),
        { label: '+ New session…', run: () => newSessionDialog() },
        hosts ? { group: 'Host shells' } : false,
        ...(hosts ?? []).map((x) => ({ id: x.id, label: h('span', { class: 'ap-row' }, shell(), ' ', markBidi(`Host shell · ${x.title}`)), search: `host shell ${x.title}`,
            title: `a login shell on this Mac - no sandbox · ${x.path}`, run: () => dispatch({ t: 'add', id: x.id }) })),
        hosts ? { label: h('span', { class: 'ap-row' }, shell(), ' ', gp ? `+ New host shell in ${state.projects.find((p) => p.key === gp)?.title || gp}` : '+ New host shell…'),
            search: 'new host shell', keys: '⌥⇧T', title: 'a login shell on this Mac - no sandbox', run: () => newGridShell() } : false,
        proj ? { group: 'Beside the active session' } : false,
        proj && !members.includes(FILES_PANE) ? { label: h('span', { class: 'ap-row' }, ic('dir'), `Files · ${pname}`), search: `files ${pname}`, keys: '⌘O', run: () => dispatch({ t: 'add', id: FILES_PANE }) } : false,
        proj && on('http') && !members.includes(HTTP_PANE) ? { label: h('span', { class: 'ap-row' }, ic('http'), `HTTP · ${pname}`), search: `http ${pname}`, run: () => dispatch({ t: 'add', id: HTTP_PANE }) } : false,
        proj && on('db') && !members.includes(DB_HERE) ? { label: h('span', { class: 'ap-row' }, ic('db'), 'Database · this project'), search: 'database this project', keys: '⌘D', run: () => dispatch({ t: 'add', id: DB_HERE }) } : false,
        on('db') ? { group: 'Database' } : false,
        on('db') ? { label: h('span', { class: 'ap-row' }, ic('db'), '+ Database pane'), search: 'database pane', title: 'a Database pane with its own connection, picked in the pane', hint: `${dbs} of ${GRID_DB_MAX}`,
            disabled: dbs >= GRID_DB_MAX ? `${GRID_DB_MAX} Database panes at most` : false, run: addDbPane } : false,
    ];
}
function addPaneMenu(anchor) {
    openMenu(anchor, addPaneEntries(), { className: 'addpane', label: 'Add pane', search: true, placeholder: 'Session, shell, Files, DB…', back: () => anchor.focus() });
}
function cancelGridAdd() {
    const back = gridAddBack;
    gridAddBack = null;
    gridAdd = false;
    if (back)
        commitLayout({ state: back.state, fx: { pick: back.pick } });
    else {
        syncLayout();
        render();
    }
}
/** focus a pane without changing the layout (grid) */
function makeActive(id) {
    if (id === active)
        return;
    unseen.delete(id);
    lastActive = active;
    active = id;
    if (composer.open())
        composer.sync();
    queue.render();
    renderMain();
    renderInspector();
}
/** on: by default the opposite of what the rail's Grid shows (pressed = the grid in the terminal view, G-N11-7) */
function toggleGrid(on) {
    dispatch(on === undefined ? { t: 'grid' } : { t: 'grid', on });
}
/** "Choose panes…": the grid's panes by kind, GRID_MAX in all, GRID_DB_MAX Database panes */
function gridChooser() {
    const sessions = state.sessions.filter((x) => !x.hidden && x.state !== 'exited');
    const hosts = hostsOn() ? hostItems(state) : null;
    // Ended sessions must neither be offered nor silently retained in the dialog's draft.
    const chosen = gridMembers().filter((id) => paneKind(id) !== 'session' || sessions.some((x) => x.id === id));
    const show = h('button', { class: 'primary', onclick: () => {
            closeModal();
            dispatch({ t: 'choose', ids: [...chosen] });
        } }, 'Show');
    const list = h('div', { class: 'plist gridchooser' });
    const draw = () => {
        show.disabled = chosen.length === 0;
        clear(list);
        const item = (id, name, note, max, path = false) => {
            const cb = h('input', { type: 'checkbox' });
            cb.checked = chosen.includes(id);
            cb.disabled = !cb.checked && (chosen.length >= GRID_MAX || max);
            cb.addEventListener('change', () => {
                const i = chosen.indexOf(id);
                if (cb.checked && i < 0)
                    chosen.push(id);
                else if (!cb.checked && i >= 0)
                    chosen.splice(i, 1);
                draw();
                list.querySelector(`input[data-id="${CSS.escape(id)}"]`)?.focus();
            });
            cb.dataset.id = id;
            // a host shell's cwd gives way to its name (a path), a state / connection note keeps its width (N10-A-10)
            return h('label', { class: 'pitem' }, h('span', { class: 'name' }, cb, ' ', name), h('span', { class: path ? 'mut' : 'mut gstate', title: path ? note : undefined }, note));
        };
        list.append(h('div', { class: 'mut small sgroup' }, 'Sessions'), ...(sessions.length ? sessions.map((x) => item(x.id, x.kind === 'advisor' ? 'Global agent' : x.title, LABEL[x.state] ?? x.state, false)) : [h('p', { class: 'mut small' }, 'No session')]));
        if (hosts) {
            // D5: running shells (and chosen ones), the ended rest folded away
            const { live: up, ended } = splitHosts(hosts, chosen);
            const row = (x) => item(x.id, `HOST · ${x.title}`, exitText(x) || tildePath(x.path, state.host_terms?.user ?? ''), false, !exitText(x));
            list.append(h('div', { class: 'mut small sgroup' }, 'Host shells · no sandbox'), ...(up.length ? up.map(row) : [h('p', { class: 'mut small' }, kbd('No host shell running - ⌥⇧T in the grid opens one into it'))]));
            if (ended.length)
                list.append(h('details', { class: 'ht-endfold' }, h('summary', { class: 'mut small' }, `Ended (${ended.length})`), ...ended.map(row)));
        }
        // 9.37: ⌘O / ⌘D's panes too - kept on Show, so they must be visible and removable here
        const proj = session(active)?.kind === 'project';
        const here = [proj || chosen.includes(FILES_PANE) ? item(FILES_PANE, 'Files', 'the active session\'s project · ⌘O', false) : null,
            on('http') && (proj || chosen.includes(HTTP_PANE)) ? item(HTTP_PANE, 'HTTP requests', 'the active session\'s project', false) : null,
            on('db') && (proj || chosen.includes(DB_HERE)) ? item(DB_HERE, 'Database · this project', 'follows the active session · ⌘D', false) : null].filter((x) => !!x);
        if (here.length)
            list.append(h('div', { class: 'mut small sgroup' }, 'Beside the active session'), ...here);
        if (on('db')) {
            const dbs = chosen.filter((x) => paneKind(x) === 'db' && x !== DB_HERE).length;
            list.append(h('div', { class: 'mut small sgroup' }, `Database · max ${GRID_DB_MAX}`), ...Array.from({ length: GRID_DB_MAX }, (_, i) => `db:${i + 1}`)
                .filter((id) => dbPanes.has(id) || chosen.includes(id)).map((id) => {
                const c = dbPanes.get(id)?.view.current();
                return item(id, c ? `${c.project} · ${c.label}` : 'Choose a database…', `Panel ${id.slice(3)}`, dbs >= GRID_DB_MAX);
            }));
            const next = nextDbPane([...dbPanes.keys(), ...chosen]);
            if (next)
                list.append(h('button', { class: 'grid-db-add', title: 'Choose a connection in the new panel', disabled: chosen.length >= GRID_MAX, onclick: () => {
                        chosen.push(next);
                        draw();
                        list.querySelector(`input[data-id="${next}"]`)?.focus();
                    } }, h('span', { class: 'grid-db-add-icon', 'aria-hidden': 'true' }, '+'), ' Add database panel'));
        }
    };
    draw();
    modal(`Panes in the grid (max ${GRID_MAX})`, list, h('div', { class: 'row' }, button('Cancel', closeModal), show));
    list.querySelector('input:not([disabled])')?.focus(); // N10-A-7: the keyboard starts in the dialog (it stayed on body)
}
function readStore(key) {
    try {
        return window.localStorage.getItem(key);
    }
    catch {
        return null;
    }
}
function writeStore(key, value) {
    try {
        window.localStorage.setItem(key, value);
    }
    catch {
        /* private window: lasts until reload */
    }
}
/** W12: `piper.layout`, the five keys before it migrated once (and removed) */
function loadLayout() {
    const l = migrateLayout(readStore, window.innerWidth);
    if (readStore(LAYOUT_KEY) === null) {
        writeStore(LAYOUT_KEY, JSON.stringify(persisted(l, false)));
        if (readStore(LAYOUT_KEY) !== null)
            for (const k of LEGACY_KEYS) {
                try {
                    window.localStorage.removeItem(k);
                }
                catch { /* ok */ }
            }
    }
    return l;
}
/** the booleans the renderers read, from the layout */
function syncLayout() {
    // 9.45: a grid of one pane is Single (the Add cell aside); a zoomed Files / Database / host pane is drawn by the grid
    grid = gridShown(lay, 'term', gridMembers().length, gridAdd) || !!zoomed();
    queueStrip = stripShown(lay);
    inspector = lay.right === 'context';
    drawer = lay.right === 'agent';
}
/** an ended session keeps its pid (its scrollback stays readable) - not a live ⌘\ partner (W12 #3) */
const livePty = (x) => hasPty(x) && x.state !== 'exited' && x.state !== 'error';
function layoutCtx() {
    const cur = session(active);
    return {
        view, active, lastActive, ids: state.sessions.map((x) => x.id), gridIds: gridMembers(), gridOther: gridMembers().some((x) => paneKind(x) !== 'session'),
        shown: view === 'term' ? grid ? gridIdsShown() : cur && hasPty(cur) ? [active] : [] : [], adding: gridAdd, focus: gridFocus,
        hasPty: (id) => { const x = session(id); return !!x && livePty(x); },
        hasSession: (id) => !!session(id),
        isEnded: (id) => { const s = session(id); return !!s && ['exited', 'error'].includes(s.state); },
        splitBlock: (id) => {
            const x = id ? session(id) : undefined;
            return x && hasPty(x) && !livePty(x) ? 'this session has no terminal (blocked, starting or ended)' : splitBlock(cur, x);
        },
        paneBlock: (pane) => (!cur ? 'Open a session first' : cur.kind !== 'project' ? 'Files, HTTP and Database panes belong to a project session'
            : pane === DB_HERE && !on('db') ? 'The Database module is off (Settings > Features)'
                : pane === HTTP_PANE && !on('http') ? 'The HTTP module is off (Settings > Features)' : ''),
        gridOn: on('grid'), feedOn: !!state.feed?.on, sessionView: !GLOBAL_VIEWS.has(view) && !!cur,
        narrow: matchMedia('(max-width: 640px)').matches,
    };
}
/** the new layout in place: stored, the booleans, the drawer, the zen pill (the caller renders) */
function setLayout(next) {
    if (next === lay)
        return false;
    lay = next;
    writeStore(LAYOUT_KEY, JSON.stringify(persisted(lay, !!state.gui_flags?.restore_view)));
    syncLayout();
    zenPill.hidden = !lay.zen;
    return true;
}
/** an action without a render of its own (activate, a view switch - their caller renders) */
function applyLayout(a) {
    const r = layoutReduce(lay, a, layoutCtx());
    if (r.fx.pick)
        storePick(r.fx.pick); // 9.45: a live session picked in the queue takes the focused session pane
    if (setLayout(r.state))
        drawDrawer(false);
    else
        syncLayout();
}
/** after a poll: a module / the Feed / the policy went off (I5) */
function reconcileLayout() {
    const r = layoutReduce(lay, { t: 'reconcile' }, layoutCtx());
    if (!setLayout(r.state))
        return;
    drawDrawer(false);
    if (r.fx.toast)
        toast(r.fx.toast);
}
/** W12: every layout input (keys, rail, header, palette) - the reducer decides, this carries out the effects */
function dispatch(a) {
    const c = layoutCtx();
    const first = layoutReduce(lay, a, c);
    if (first.fx.refuse)
        return toast(first.fx.refuse);
    // the terminal view first: the result from where it was asked (⌘\ in Files shows the 2-pane grid, it does not close it)
    const run = () => commitLayout(layoutReduce(lay, a, c));
    if (first.fx.toTerm && view !== 'term') {
        if (toTerm(run))
            run();
        return;
    }
    commitLayout(first);
}
function commitLayout(r) {
    const was = drawer;
    if (r.fx.add && !gridAddBack && lay.center.kind === 'single')
        gridAddBack = { state: lay, pick: gridMembers() };
    if (!r.fx.add && (r.fx.pick || r.state.center.kind === 'single'))
        gridAddBack = null;
    if (r.fx.pick)
        storePick(r.fx.pick); // D1/D2/9.45: every add / remove edits the grid's panes
    const wasAdd = gridAdd;
    gridAdd = r.state.center.kind === 'grid' && (r.fx.add ?? (r.fx.pick ? false : gridAdd));
    if (gridAdd && !wasAdd && r.fx.add)
        slotFresh = true;
    if (!setLayout(r.state))
        syncLayout();
    if (r.fx.next) { // 9.45: ✕ on the focused pane - its neighbour takes the focus
        if (paneKind(r.fx.next) === 'session') {
            gridFocus = '';
            if (r.fx.next !== active) {
                lastActive = active;
                active = r.fx.next;
            }
        }
        else
            gridFocus = r.fx.next;
    }
    if (!gridIdsShown().includes(gridFocus))
        gridFocus = '';
    if (grid && view === 'term' && !zoomed()) { // the grid shows: its pane is the active one
        const panes = gridSessions();
        if (panes.length && !panes.some((x) => x.id === active))
            active = panes[0].id;
    }
    if (composer.open())
        composer.sync();
    if (r.fx.hub)
        setView(session(active) || !moduleOn(state, 'gitlab') ? 'term' : 'gitlab'); // a module that is off has no view
    for (const t of terms.values())
        t.freeze(true);
    render();
    drawDrawer(was !== drawer);
    if (r.fx.toast)
        toast(r.fx.toast);
    if (r.fx.focus === 'queue')
        queue.focus();
    else if (r.fx.focus === 'detail')
        focusDetail();
    window.setTimeout(() => { for (const t of terms.values())
        t.freeze(false); }, 200);
}
function toggleSplit() {
    dispatch({ t: 'split' }); // D2: the 2-pane grid of the active + the previous session; on screen = off
}
/** `id` side by side with the active session - a 2-pane grid (from the rail's menu; ⌘\ pairs with the previous one) */
function splitWith(id) {
    unseen.delete(id);
    dispatch({ t: 'splitWith', id });
}
/** a KeyGate of shortcuts.ts: a module, or the GUI condition behind a key (D15) */
function gateOn(g) {
    return !g ? true : g === 'feed' ? !!state.feed?.on : g === 'nav' ? navOn() : g === 'inbox' ? viewOn('gitlab') : g === 'host' ? hostsOn() : on(g);
}
/** a global key is live while its Keys row is shown - the handler and the dialog read the same registry */
const keyOn = (k) => gateOn(keyGate(k));
function shortcuts() {
    const swap = !!state.gui_flags?.term_click_opens_files; // policy term_click_opens_files
    const groups = shownShortcuts(gateOn);
    const desc = (r) => (swap && r.k === 'click on a path' ? 'a menu: Files ↵, beside ⇧↵, IntelliJ, copy (⌘+click: Files tab, ⇧+click: IntelliJ)' : r.d);
    // N4 (F18 / T1): a sheet - the header with a filter and × stays, the groups flow in columns, each row is
    // what it does on the left and its key chips on the right
    const cols = h('div', { class: 'ks-cols' });
    const body = h('div', { class: 'ks-body' }, cols); // the body scrolls, the columns inside it only flow (a column box with a height would overflow sideways)
    const draw = (q) => {
        const shown = filterShortcuts(groups, q);
        cols.replaceChildren(...shown.map((g) => h('section', { class: 'ks-group' }, h('h4', {}, g.title), ...g.rows.map((r) => {
            const m = g.ctrl || r.ctrl ? 'Ctrl' : 'Alt';
            return h('div', { class: 'ks-row' }, h('span', { class: 'ks-d' }, kbd(desc(r), m)), h('span', { class: 'ks-k' }, ...keyChips(r.k).map((c) => c.kind === 'key' ? h('kbd', {}, kbd(c.t, m))
                : h('span', { class: c.kind === 'sep' ? 'ks-sep' : 'ks-t' }, kbd(c.t, m)))));
        }))));
        if (!shown.length)
            cols.append(h('p', { class: 'ks-none' }, 'No key matches “' + q.trim() + '”.'));
    };
    const find = h('input', { type: 'search', class: 'ks-find', placeholder: 'Filter keys', 'aria-label': 'Filter keys',
        oninput: () => draw(find.value) });
    draw('');
    modal('Keyboard shortcuts', h('div', { class: 'keysheet' }, h('div', { class: 'ks-head' }, h('h3', {}, 'Keyboard shortcuts'), find, h('button', { class: 'ks-x', 'aria-label': 'Close', title: 'Close (Esc)', onclick: closeModal }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }))), body));
    find.focus();
}
function commands() {
    const cmds = [];
    // F18: Sessions · Actions · Go to · Ended; a row without a group is an action
    const goTo = [];
    const go = (c) => goTo.push({ ...c, group: 'Go to' });
    // what you do most first: switch (the ones that wait for you on top), then views and actions; the ended sessions last
    const order = [...state.sessions].filter((s) => !s.hidden).sort((a, b) => Number(!NEEDS_YOU.includes(a.state)) - Number(!NEEDS_YOU.includes(b.state)) || Number(a.state === 'exited') - Number(b.state === 'exited'));
    // D12: one row per session (⏎ switches), its actions behind →; the ended ones are a group at the end
    const ended = [];
    for (const s of order) {
        const name = s.kind === 'advisor' ? 'Global agent' : s.title;
        const history = [s.kind === 'advisor' ? 'all projects' : s.project, s.branch, s.first_prompt, s.last_prompt].filter(Boolean).join(' · ');
        const sub = [{ label: 'Switch to it', run: () => select(s.id) }];
        if (canStop(s))
            sub.push({ label: 'Stop…', run: () => confirmStop(s) });
        const switchTo = s.claude_session ? switchTarget(s, on('codex')) : null;
        if (switchTo)
            sub.push({ label: `Continue with ${agentName(switchTo)}…`, hint: 'stops it, hands the conversation over as a file', run: () => confirmSwitch(s) });
        if (['exited', 'error', 'blocked'].includes(s.state))
            sub.push({ label: continueLabel(s), hint: history, run: () => void act(s, 'retry') });
        // F18: state · branch, never the raw prompt (it still matches); ⇧↵ = beside the active one
        // 9.141: one a restart interrupted stays among the Sessions, marked, until restored or dismissed
        const intr = s.state === 'exited' && !!s.interrupted;
        const row = { label: name, icon: dot(s), hint: [LABEL[s.state] ?? s.state, s.branch].filter(Boolean).join(' · '), search: history, run: () => select(s.id), sub,
            ...(hasTerminal(s) && s.id !== active && on('grid') ? { beside: () => splitWith(s.id) } : {}) };
        if (intr)
            cmds.push({ ...row, group: 'Sessions', hint: ['interrupted · ' + wasWord(s.interrupted.was), s.branch].filter(Boolean).join(' · '), search: history + ' interrupted restart' });
        else if (['exited', 'error'].includes(s.state))
            ended.push({ ...row, group: 'Ended', folded: true });
        else
            cmds.push({ ...row, group: 'Sessions' });
    }
    if (!state.sessions.some((s) => s.kind === 'advisor' && LIVE.has(s.state))) {
        cmds.push({ label: 'Start the Global agent', run: () => void start('advisor', '', ''), module: 'agent' });
    }
    cmds.push({ label: 'Resume conversation…', hint: 'search saved Claude and Codex conversations in all projects; resume or fork', run: () => showResume() });
    cmds.push({ label: 'New session…', hint: 'with a task', run: () => newSessionDialog() });
    if (!rebuildState().by)
        cmds.push(rebuildArmed() // while a start builds the image: nothing to arm or cancel
            ? { label: 'Cancel the pending image rebuild', hint: 'the next start keeps the current image', run: () => void setRebuild(false) }
            : { label: 'Rebuild sandbox image at the next start', hint: 'a new agent CLI and apt packages, several minutes', run: () => void setRebuild(true) });
    // A1: the inbox and "from an issue" follow the forge modules (GitLab or GitHub), like the rail - not gitlab alone
    const forge = viewOn('gitlab') ? inboxName(state.inbox) : '';
    if (forge)
        cmds.push({ label: `New session from ${forge === 'Inbox' ? 'an' : `a ${forge}`} issue…`, hint: 'task from the issue and its comments', run: () => newSessionDialog(true) });
    // F18: the four "Queue: …" rows are one, their variants behind ›
    const qsub = [{ label: 'The whole queue', hint: 'Overview icon', run: () => dispatch({ t: 'overview' }) }];
    if (queueShown())
        qsub.push({ label: 'Hide (a wide terminal)', run: () => toggleQueue(false) });
    if (queueShown() && !stripOn())
        qsub.push({ label: 'Fold to the rail', hint: '⌘B', run: () => toggleStrip() });
    qsub.push({ label: 'Focus it', hint: '⌘⇧L', run: focusQueue });
    go({ label: 'Queue', hint: 'fold / focus', keys: queueShown() && !stripOn() ? '⌘B' : '⌘⇧L', search: 'queue overview hide fold focus', run: () => dispatch({ t: 'overview' }), sub: qsub });
    if (state.feed?.on)
        go({ label: 'Chat with the Global agent', hint: '⌘0', run: () => toggleDrawer(true) });
    if (state.inbox && forge) {
        const act = { focus: () => inbox.focus(), refresh: () => { setView('gitlab'); inbox.refresh(); }, rules: () => inbox.openRules() };
        for (const c of inboxCommands(forge, on('gitlab'), state.inbox.new))
            (c.act === 'focus' ? go : (x) => cmds.push(x))({ label: c.label, hint: c.hint, run: act[c.act] });
    }
    if (state.prod?.on) {
        go({ label: 'Production check', hint: 'the report of the morning check', run: () => setView('prod') });
        cmds.push({ label: 'Check production', hint: 'the morning production check of errors and alerts, now', run: () => { setView('prod'); prod.run('check'); } });
    }
    for (const c of layoutCommands(lay, view, GRID_MAX, gridMembers().length, gridAdd, restoreMembers(layoutCtx()).length))
        cmds.push({ label: c.label, hint: c.hint, run: () => dispatch(c.action), ...(c.module ? { module: c.module } : {}) });
    cmds.push({ label: 'Grid: choose panes…', hint: 'sessions, host shells, Database', run: gridChooser, module: 'grid' });
    if (hostsOn())
        cmds.push({ label: 'Grid: add host shell', hint: '⌥⇧T in the grid · no sandbox', run: () => newGridShell(), module: 'grid' });
    if (on('db'))
        cmds.push({ label: 'Grid: add Database pane', hint: `its own connection · max ${GRID_DB_MAX}`, run: addDbPane, module: 'grid' });
    cmds.push({ label: 'Next session waiting for you', hint: '⌘J', run: nextWaiting });
    if (railOrder.length)
        cmds.push({ label: 'Running: default session order', hint: 'drops the manual order', run: () => saveRailOrder([]) });
    // N9: the views of a session have nothing to show without one (an empty page): offered only with a session open
    const shown0 = session(active);
    if (shown0)
        go({ label: 'Project files', hint: 'browser', run: () => setView('files') });
    go({ label: 'HTTP requests', hint: '.http files', run: () => setView('http'), module: 'http' });
    go({ label: 'Database', hint: 'tables, SQL console, connections', run: () => openDb(), module: 'db' });
    if (state.host_terms?.on) { // W2
        go({ label: 'Host shells', hint: `${kbd('⌥F12')} · login shells on this Mac, no sandbox`, run: () => toggleHost() });
        for (const p of state.projects.filter((x) => x.exists))
            go({ label: `Host shell: ${p.title || p.key}`, hint: 'no sandbox', run: () => openHostShell(p.key) });
    }
    go({ label: 'Open file…', hint: '⌘P', run: quickOpen });
    // D1: Files / the project's Database as a grid pane next to the session
    if (shown0?.kind === 'project')
        cmds.push({ label: filesPaneShown() ? 'Files pane: close' : 'Files pane beside the session', hint: '⌘O', run: () => dispatch({ t: 'pane', pane: FILES_PANE }) });
    if (shown0?.kind === 'project' && on('http'))
        cmds.push({ label: paneShown(HTTP_PANE) ? 'HTTP pane: close' : 'HTTP pane beside the session (this project\'s .http requests)', run: () => dispatch({ t: 'pane', pane: HTTP_PANE }) });
    if (shown0?.kind === 'project' && on('db'))
        cmds.push({ label: paneShown(DB_HERE) ? 'Database pane: close' : 'Database pane beside the session (this project: dev / test / prod)', hint: '⌘D', run: () => dispatch({ t: 'pane', pane: DB_HERE }) });
    if (shown0)
        go({ label: 'Search the project…', hint: '⌘⇧F', run: () => { setView('files'); files.searchMode(); } });
    if (filesShown() && files.hasFile()) {
        cmds.push({ label: 'File: diff against HEAD', hint: '⌘⇧D', run: () => files.toggleDiff() });
        cmds.push({ label: 'File: find usages of the identifier at the caret', hint: '⌥F7', run: () => files.findUsages() });
        cmds.push({ label: 'File: insert the reference into the prompt', hint: '⌥⏎', run: () => files.insertRef() });
    }
    if (filesShown())
        cmds.push({ label: 'Files: tool panel (usages, search, problems, diagnostics, turns)', hint: kbd('⌘⌥↓', 'Ctrl'), run: () => files.togglePanel() });
    if (shown0)
        go({ label: 'Conversation', hint: 'active session, readable transcript', run: () => setView('convo') });
    const act0 = session(active);
    if (act0 && act0.pid !== null && LIVE.has(act0.state) && !act0.tty) {
        cmds.push({ label: attachLabel(state.terminal_app), hint: 'piper attach (Settings > Terminal)', run: () => void openInTerminal(act0) });
    }
    cmds.push({ label: 'Search the conversations of all sessions…', run: () => convo.searchAll() });
    if (shown0)
        go({ label: 'Session history', run: () => setView('history') });
    go({ label: 'Piper audit', hint: 'audit.log', run: () => setView('audit') });
    go({ label: 'Token usage', hint: 'per day and project', run: () => setView('usage'), module: 'usage' });
    go({ label: 'Timesheet', hint: 'time by issue and day', run: () => setView('timesheet'), module: 'timesheet' });
    go({ label: 'Settings', hint: '⌘, · features, GitLab, projects, isolation', run: () => setView('settings') });
    go({ label: 'Setup: installation check & features', hint: 'what this machine needs, which modules you see', run: () => setView('setup') });
    const cur = session(active);
    if (cur?.kind === 'project') {
        go({ label: 'Git window', hint: `${cur.project} · branches, worktrees, the log`, run: () => git.gitWindow(cur.project) });
        cmds.push({ label: 'Git: what the last turn did', run: () => git.showTurn(cur.id) });
    }
    if (on('composer'))
        cmds.push({ label: 'Composer', hint: '⌘E', run: () => toggleComposer() });
    go({ label: 'Recent alerts', hint: 'the last toasts and desktop notices (the bell)', run: () => openNotices() });
    go({ label: 'Keyboard shortcuts', hint: 'Keys in the activity bar', run: shortcuts });
    cmds.push({ label: 'Mattermost: notifications and test', hint: 'incoming webhook', run: () => void openMattermost(modal, toast), module: 'mattermost' });
    if (shown0)
        go({ label: 'Context panel: issue, Git, diagnostics, isolation', hint: '⌘I', run: toggleInspector });
    const dash = window.__PIPER__?.dashboardUrl;
    if (dash && state.stack_up !== false)
        go({ label: 'Open the stacks dashboard', hint: dash, run: () => window.open(dash, 'dev-stacks', 'noopener'), module: 'stack' });
    // D12: no "New session in <project>" rows - New session… picks the project
    const sessions = cmds.filter((c) => c.group === 'Sessions');
    const actions = cmds.filter((c) => c.group !== 'Sessions').map((c) => ({ ...c, group: 'Actions' }));
    return keepOn([...sessions, ...actions, ...goTo, ...ended], on);
}
function palette() {
    const cmds = commands();
    openPalette((el, focus) => {
        mountOverlay('Commands', false, el);
        focus.focus();
    }, closeModal, cmds);
}
boot();
/** 9.107: true the first time this browser sees the key (storage blocked = every time) */
function firstTimeHere(key) {
    try {
        if (localStorage.getItem(key))
            return false;
        localStorage.setItem(key, '1');
    }
    catch { /* private window: show it */ }
    return true;
}
/** 9.109: a phone-width window (the N10 queries, 9.108) */
function phoneWidth() {
    return matchMedia('(max-width: 640px)').matches;
}
//# sourceMappingURL=main.js.map