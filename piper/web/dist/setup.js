// Setup: one page that says why Piper, whether this
// machine is ready (one line while it is; a problem opens itself and comes first), which parts of Piper
// you want (presets that promise, a gallery with a screen per module) and how the first session starts.
// Same data as before: the doctor rows (/api/doctor) and the feature modules (/api/features). It stands
// in the main area instead of "No session" until the user is done with it (a new install) or while a
// check fails; Settings → Features and the palette open it any time (then without the welcome parts; 9.38: the
// presets and the gallery stay - D10 had them only in the welcome, the user wants them in the check too).
// Pure helpers first (node tests): which module is on, what the readiness line says, the preset chips.
import { call } from './api.js';
import { kbd } from './clipkeys.js';
import { clear, h } from './dom.js';
import { REBUILD_HINT } from './text.js';
/** a module is on unless piperd says it is off (an older piperd sends no features: all on) */
export function moduleOn(st, m) {
    const mods = st.features?.modules;
    // no summary (an older piperd) = everything on; a module it does not list (an extension's, not loaded) = off
    return !mods ? true : m in mods ? mods[m] !== false : false;
}
/** keep what has no module or whose module is on */
export function keepOn(items, on) {
    return items.filter((x) => !x.module || on(x.module));
}
/** the card replaces the empty main area: not onboarded yet, or a check that blocks sessions */
export function setupWanted(st, rep) {
    return (!!st.features && !st.features.onboarded) || (!!rep && rep.fail > 0);
}
/** Direct execution does not need Docker or a Hole image; older servers default to Hole. */
export function setupUsesHole(runtime) { return runtime !== 'direct'; }
/** how many modules are off (the empty state's "n modules are off" link) */
export function offModules(f) {
    return f ? f.list.filter(([id]) => f.modules[id] === false).length : 0;
}
/** modules some preset decides - the host shells are in none (D10: switched on deliberately in Settings › Features) */
export function presetModules(f) {
    const any = new Set(Object.values(f.preset_modules ?? {}).flat());
    return f.list.map(([id]) => id).filter((id) => any.has(id));
}
/** the chips of a preset card: its modules' labels; Everything says so in one chip */
export function presetChips(f, preset) {
    const ids = f.preset_modules?.[preset] ?? [];
    if (ids.length && ids.length === presetModules(f).length)
        return [`all ${ids.length} modules`];
    const label = new Map(f.list.map(([id, l]) => [id, l]));
    return ids.map((id) => label.get(id) ?? id);
}
/** the preset these module values amount to, else 'custom' (features.matching_preset; the host shells are in none) */
export function matchingPreset(f, mods) {
    const decided = presetModules(f);
    const hit = f.presets.find(([id]) => decided.every((m) => (f.preset_modules?.[id] ?? []).includes(m) === (mods[m] === true)));
    return hit ? hit[0] : 'custom';
}
/** F19: what these module values are in words - the matching preset, or the nearest one and what differs
 *  ("Custom = Developer + Grid HTTP − Diagnostics"); labels, in the list's order. A tie goes to the earlier preset. */
export function presetDiff(f, mods) {
    const decided = presetModules(f);
    const label = new Map(f.list.map(([id, l]) => [id, l]));
    let best = null;
    for (const [id, name] of f.presets) {
        const inP = new Set(f.preset_modules?.[id] ?? []);
        const added = decided.filter((m) => mods[m] === true && !inP.has(m)).map((m) => label.get(m) ?? m);
        const removed = decided.filter((m) => mods[m] !== true && inP.has(m)).map((m) => label.get(m) ?? m);
        if (!best || added.length + removed.length < best.added.length + best.removed.length)
            best = { preset: id, base: id, label: name, added, removed };
    }
    if (!best)
        return { preset: 'custom', base: '', label: '', added: [], removed: [] };
    return { ...best, preset: best.added.length || best.removed.length ? 'custom' : best.base };
}
/** F19: a module as a row (Settings › Features) - the sketch small, label, one line, switch; a click on the row
 *  opens the sketch large under it (the gallery stays the welcome's, moduleTile) */
export function moduleRow(id, label, text, on, flip, o = {}) {
    const thumb = h('img', { src: `/piper/img/modules/${id}.svg`, alt: '', loading: 'lazy' });
    thumb.addEventListener('error', () => thumb.remove());
    const sw = h('button', { class: 'su-switch', role: 'switch', 'aria-checked': String(on), 'data-fk': `sw:${id}`, 'aria-label': (on ? 'turn off ' : 'turn on ') + label,
        onclick: (e) => { e.stopPropagation(); flip(); } }, h('span', { class: 'su-knob' }));
    const head = h('div', { class: 'fm-head', role: 'button', tabindex: '0', 'aria-expanded': String(!!o.open), title: o.open ? 'hide the picture' : 'show what it looks like',
        onclick: () => o.toggle?.() }, h('span', { class: 'fm-thumb', 'aria-hidden': 'true' }, thumb), h('span', { class: 'fm-text' }, h('span', { class: 'fm-title' }, h('b', {}, label), o.badge ?? null), h('span', { class: 'mut small' }, kbd(text))), sw);
    head.addEventListener('keydown', (e) => { if (e.target === head && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        o.toggle?.();
    } });
    let big = null;
    if (o.open) {
        const img = h('img', { src: `/piper/img/modules/${id}.svg`, alt: `${label}: what it looks like` });
        img.addEventListener('error', () => img.remove());
        big = h('div', { class: 'fm-big' }, img);
    }
    return h('div', { class: 'fm-row' + (on ? '' : ' off') + (o.cls ? ' ' + o.cls : ''), 'data-module': id }, head, big);
}
/** a preset card (the welcome / the check and Settings › Features, 9.40) */
export function presetCard(f, [id, label, text], on, pick) {
    return h('button', { class: 'su-preset' + (on ? ' on' : ''), role: 'radio', 'aria-checked': String(on), 'data-fk': `preset:${id}`, onclick: pick }, h('div', { class: 'su-preset-head' }, h('b', {}, label), on ? h('span', { class: 'ok small' }, '✓ selected') : null), h('span', { class: 'mut small' }, text), h('div', { class: 'su-chips' }, ...presetChips(f, id).map((c) => h('span', { class: 'pill tiny' }, c))));
}
/** a module's tile: its sketch (img/modules/<id>.svg, dummy data), label, switch and what it adds; off = dimmed.
 *  `cls` / `badge` / `note`: the setup card's check note, Settings' "isolation" badge and its unsaved mark (9.40) */
export function moduleTile(id, label, text, on, flip, o = {}) {
    const img = h('img', { src: `/piper/img/modules/${id}.svg`, alt: '', loading: 'lazy' });
    img.addEventListener('error', () => img.remove()); // no screen for it: the label underneath shows
    const sw = h('button', { class: 'su-switch', role: 'switch', 'aria-checked': String(on), 'data-fk': `sw:${id}`, 'aria-label': (on ? 'turn off ' : 'turn on ') + label,
        onclick: flip }, h('span', { class: 'su-knob' }));
    return h('div', { class: 'su-tile' + (on ? '' : ' off') + (o.cls ? ' ' + o.cls : ''), 'data-module': id }, h('div', { class: 'su-shot' }, h('span', { class: 'su-shot-ph', 'aria-hidden': 'true' }, label), img), h('div', { class: 'su-tile-body' }, h('div', { class: 'su-tile-head' }, h('b', {}, label), o.badge ?? null, sw), h('div', { class: 'mut small' }, kbd(text)), o.note ?? null));
}
/** fail and warn need the user (failures first); an `info` row belongs to an optional module and shows on its
 *  tile only, so it never makes the machine look less ready */
export function splitRows(rep) {
    const problems = rep.rows.filter((r) => !r.ok && r.level !== 'info').sort((a, b) => (a.level === 'fail' ? 0 : 1) - (b.level === 'fail' ? 0 : 1));
    return { problems, passed: rep.rows.filter((r) => r.ok), info: rep.rows.filter((r) => !r.ok && r.level === 'info') };
}
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
/** the one line that stands for the whole check while nothing is wrong */
export function readiness(rep) {
    const { problems, passed } = splitRows(rep);
    const count = `${passed.length} of ${passed.length + problems.length} checks passed`;
    if (rep.fail) {
        const warns = problems.filter((r) => r.level !== 'fail').length;
        return { tone: 'fail', title: plural(rep.fail, 'blocker') + (warns ? `, ${plural(warns, 'warning')}` : ''), detail: count };
    }
    if (problems.length)
        return { tone: 'warn', title: 'This machine works, with ' + plural(problems.length, 'warning'), detail: count };
    const facts = passed.filter((r) => ['hole', 'docker', 'projects'].includes(r.key)).map((r) => r.title);
    return { tone: 'ok', title: 'This machine is ready', detail: [count, ...facts].join(' · ') };
}
/** a doctor row of an optional module shows on that module's tile too (ijhttp → HTTP requests …) */
const MODULE_ROW = { gitlab: 'gitlab', stack: 'stack', ijhttp: 'http', db: 'db', mattermost: 'mattermost' };
const MARK = { ok: '✓', fail: '✕', warn: '!', info: 'i' };
// F25: the welcome's first impression in one row of three tiles (a sentence each), above the stepper
const VALUES = [
    ['Parallel sessions', 'One queue shows what waits for you.'],
    ['Execution options', 'Run with native permissions, or choose Hole isolation.'],
    ['You stay in control', 'Answer agent prompts in the terminal and Piper tool approvals on cards.'],
];
const STEPPER = ['Check this machine', 'Pick modules', 'Start a session'];
const STEPS = [
    ['Pick a project', 'every git checkout under PROJECT_PATH is offered'],
    ['Write the task', 'or pick an issue from the inbox'],
    ['Confirm agent access', 'answer native login and project trust in the terminal; Hole may build an image first'],
];
/** 9.54: the Setup row of hole's agent image - one image for every project and session.
 *  `pending`: a rebuild is armed; `by`: the starting session that took it ('' = the next start will) */
export function imageLine(info, pending, by = '') {
    const img = info?.ok ? info.images?.[0] : undefined;
    const ref = img ? img.ref.replace(/^hole-sandbox\//, '').replace(/:([0-9a-f]{8})[0-9a-f]+$/, ':$1') : '';
    if (pending) {
        const what = by ? `rebuilding now in ${by}` : 'rebuild pending - the next start of any session';
        return { text: [what, ref, img?.size].filter(Boolean).join(' · '), tone: 'warn', action: by ? '' : 'cancel' }; // a build under way: Cancel would stop nothing
    }
    if (!info)
        return { text: 'looking…', tone: 'mut', action: '' };
    if (!info.ok)
        return { text: `docker: ${info.error ?? 'no answer'}`, tone: 'warn', action: '' };
    if (!img)
        return { text: 'not built yet - the first session start builds it', tone: 'mut', action: '' };
    const used = info.in_use ? `in use by ${info.in_use} sandbox${info.in_use === 1 ? '' : 'es'}` : 'not in use';
    const older = (info.images?.length ?? 1) - 1;
    // information only (9.54 S1): the rebuild is armed in ⌘K for the next start
    return { text: [ref, img.size, `built ${img.age}`, used, older ? `+${older} older` : ''].filter(Boolean).join(' · '), tone: 'ok', action: '' };
}
/** 9.54: the note under an armed image row - before a start took it, and while that start builds */
export function rebuildNote(by = '') {
    return by
        ? `The image is rebuilt in ${by}: the agent CLI (Claude Code, Codex) and the apt packages are installed again. Several minutes. Running sessions keep their image; the rebuilt one serves every session started after it. The row clears when that session starts its agent.`
        : 'The next start of any session rebuilds the image: the agent CLI (Claude Code, Codex) and the apt packages are installed again. Several minutes. Running sessions keep their image; the rebuilt one serves every session started after it.';
}
/** 9.54: where the rebuild is armed (the Setup row only shows the image) */
export function rebuildWhere() {
    return `Rebuild the image from ${kbd('⌘K')} (the next start).`;
}
/** F25: how often the welcome / the check looks again by itself while something blocks sessions (0 = not) -
 *  the user starts Docker and the page notices, no "Check again" needed */
export const RECHECK_MS = 12000;
export function recheckDelay(rep, shown) {
    return shown && !!rep && rep.fail > 0 ? RECHECK_MS : 0;
}
/** F25: the check in one head line - blockers on top, the optional ones folded into one line, passed only counted */
export function checkSummary(rep) {
    const { problems, passed } = splitRows(rep);
    const blockers = problems.filter((r) => r.level === 'fail');
    const optional = problems.filter((r) => r.level !== 'fail');
    const n = blockers.length;
    return {
        tone: n ? 'fail' : optional.length ? 'warn' : 'ok',
        title: n ? `${n} thing${n === 1 ? ' blocks' : 's block'} the first session` : `This machine is ready · ${passed.length}/${passed.length + problems.length}`,
        blockers, optional, passed: passed.length,
        fold: optional.length ? `${optional.length} optional — ${optional.map((r) => r.title).join(', ')}` : 'Passed checks',
    };
}
const CMD_RX = /^(task|hole|brew|gh|sudo|open|docker|git|npm|pip3?|python3?|curl|claude|mkdir|cp|ln|systemctl|launchctl)\s/;
/** F25: a blocker's one action - Install / the mount test (piperd does it), a command to copy (the fix itself or the
 *  `command` in it; "Copy: open -a Docker" while short), else Settings; prose fixes stay a line of text */
export function rowAction(r) {
    if (r.action)
        return { kind: 'run', label: r.action === 'deep' ? 'Run the mount test' : 'Install', value: r.action };
    const cmd = r.fix.match(/`([^`]+)`/)?.[1] ?? (CMD_RX.test(r.fix + ' ') ? r.fix : '');
    if (cmd)
        return { kind: 'copy', label: cmd.length <= 24 ? `Copy: ${cmd}` : 'Copy command', value: cmd };
    if (r.settings)
        return { kind: 'settings', label: 'Settings', value: r.settings };
    return { kind: '', label: '', value: '' };
}
export function createSetup(deps) {
    const hole = () => setupUsesHole(deps.runtime?.());
    const el = h('div', { class: 'setup' });
    let rep = null;
    let img = null; // 9.54: GET /api/image, with every check
    let busy = ''; // '' | quick | deep
    let err = '';
    let passedOpen = false; // F25: the optional + passed checks and the mount test, folded by default
    let dismissed = false;
    let keepFk = ''; // N10-D-5: the control to focus once it is enabled again   // Done / Close in this page: the card goes even while a check still fails
    let step = 1; // F25: the welcome's stepper (1 Check this machine · 2 Pick modules · 3 Start a session)
    let galleryOpen = false; // F25: step 2's "What each module adds" - off the main path
    let pollT = 0; // F25: the re-check while something blocks
    const shown = () => el.isConnected && document.visibilityState !== 'hidden';
    /** F25: while a blocker stands and the card is on screen, look again by itself (poll /api/doctor?fresh=1) */
    function schedule() {
        if (pollT)
            return; // one timer; a manual check meanwhile does not restart it
        const ms = recheckDelay(rep, shown());
        if (ms)
            pollT = window.setTimeout(() => { pollT = 0; if (recheckDelay(rep, shown()))
                void check(false, true); }, ms);
    }
    function unschedule() {
        clearTimeout(pollT);
        pollT = 0;
    }
    /** fresh: Check again / the mount test / the re-check - piperd skips its caches */
    async function check(deep = false, fresh = false) {
        if (busy)
            return;
        busy = deep ? 'deep' : 'quick';
        const before = rep?.fail ?? 0;
        draw();
        try {
            rep = (await call('GET', '/api/doctor' + (deep ? '?deep=1&fresh=1' : fresh ? '?fresh=1' : ''))).data;
            err = '';
            img = hole() ? await call('GET', '/api/image').then((r) => r.data, (e) => ({ ok: false, error: String(e instanceof Error ? e.message : e) })) : null;
        }
        catch (e) {
            err = String(e instanceof Error ? e.message : e);
        }
        busy = '';
        if (before && rep && !rep.fail && shown())
            deps.toast('Nothing blocks sessions any more');
        draw();
        deps.changed();
        schedule();
    }
    async function save(body) {
        try {
            const r = await call('POST', '/api/features', body);
            if (r.data)
                deps.applied(r.data);
            deps.changed();
            if (body.preset || body.modules)
                void check(); // a module turned on brings its own checks
        }
        catch (e) {
            deps.toast('Not saved: ' + String(e instanceof Error ? e.message : e));
        }
    }
    async function action(r) {
        if (r.action === 'deep')
            return check(true);
        const path = r.action === 'http-install' ? '/api/http/install' : '/api/db/install';
        try {
            await call('POST', path, {});
            deps.toast(r.action === 'http-install' ? 'Installing ijhttp - Check again in a minute' : 'Installing the drivers - Check again in a minute');
        }
        catch (e) {
            deps.toast(String(e instanceof Error ? e.message : e));
        }
    }
    /** Done / Start first session: the card is done with, for this home and this page */
    async function finish(start) {
        await save({ onboarded: true });
        dismissed = true;
        unschedule();
        deps.done();
        if (start)
            deps.newSession();
    }
    const copy = (text) => void navigator.clipboard?.writeText(text).then(() => deps.toast('Copied'));
    function fixEl(r) {
        return r.fix ? h('div', { class: 'su-fix small' }, 'Fix: ', h('code', {}, r.fix), h('button', { class: 'mini', title: 'copy', onclick: () => copy(r.fix) }, 'Copy')) : null;
    }
    function actionBtn(r) {
        return r.action ? h('button', { class: 'mini', disabled: !!busy, onclick: () => void action(r) }, r.action === 'deep' ? 'Run the mount test' : 'Install') : null;
    }
    function rowEl(r) {
        const state = r.ok ? 'ok' : r.level;
        return h('li', { class: `su-row su-${state}` }, h('span', { class: 'su-mark', 'aria-label': state }, MARK[state] ?? ''), h('div', { class: 'su-text' }, h('div', { class: 'su-title' }, r.title), r.text ? h('div', { class: 'mut small' }, r.text) : null, fixEl(r)), h('div', { class: 'su-acts' }, !r.ok && r.settings ? h('button', { class: 'mini', onclick: () => deps.openSettings(r.settings) }, 'Settings') : null, actionBtn(r)));
    }
    /** F25: a blocker with its one action; a fix in words stays a line under the text */
    function blockerEl(r) {
        const a = rowAction(r);
        const btn = a.kind === 'run' ? h('button', { disabled: !!busy, onclick: () => void action(r) }, a.label)
            : a.kind === 'copy' ? h('button', { title: a.value, onclick: () => copy(a.value) }, a.label)
                : a.kind === 'settings' ? h('button', { onclick: () => deps.openSettings(a.value) }, a.label) : null;
        return h('li', { class: 'su-row su-fail su-blocker' }, h('span', { class: 'su-mark', 'aria-label': 'blocks' }, MARK.fail), h('div', { class: 'su-text' }, h('div', { class: 'su-title' }, r.title), r.text ? h('div', { class: 'mut small' }, r.text) : null, r.fix && a.kind !== 'copy' ? h('div', { class: 'mut small' }, 'Fix: ' + r.fix) : null), h('div', { class: 'su-acts' }, btn));
    }
    /** F25: the welcome's head - one sentence and the three values as a row of tiles (the first impression) */
    function heroEl() {
        return h('section', { class: 'su-hero' }, h('h1', {}, 'Welcome to Piper'), h('p', { class: 'mut' }, 'Claude and Codex sessions on your chosen machine. Three steps to the first one.'), h('div', { class: 'su-values' }, ...VALUES.map(([t, d], i) => h('div', { class: 'su-value' }, h('span', { class: 'su-value-ic', 'aria-hidden': 'true' }, String(i + 1)), h('div', {}, h('b', {}, t), h('div', { class: 'mut small' }, d))))));
    }
    function stepperEl() {
        const fails = rep?.fail ?? 0;
        return h('nav', { class: 'su-stepper', 'aria-label': 'steps' }, ...STEPPER.map((t, i) => {
            const n = i + 1;
            const done = n < step && !(n === 1 && fails);
            return h('button', { class: 'su-step' + (n === step ? ' on' : '') + (done ? ' done' : ''), 'aria-current': n === step ? 'step' : 'false',
                'data-fk': `step:${n}`, onclick: () => { step = n; draw(); } }, h('span', { class: 'su-step-n', 'aria-hidden': 'true' }, done ? '✓' : String(n)), t);
        }));
    }
    /** F25: the check - blockers on top with one action each, the optional ones one folded line, passed only counted;
     *  ready = one line "This machine is ready · 14/14" with Details (the passed list and the mount test) */
    function checkEl() {
        const box = h('section', { class: 'su-ready su-check' });
        if (busy && !rep) { // only the first check: later ones keep the last report on screen (a click must not make the page jump)
            box.append(h('div', { class: 'su-ready-head' }, h('span', { class: 'spinner' }), h('b', {}, busy === 'deep' ? 'Running the mount test…' : 'Checking this machine…'), h('span', { class: 'mut small' }, busy === 'deep' ? 'a short container, a few seconds (K18)' : 'runtime prerequisites, git, Python, agents and projects')));
            return box;
        }
        if (err)
            box.append(h('p', { class: 'warn' }, err));
        if (!rep)
            return box;
        const sm = checkSummary(rep);
        const facts = readiness(rep).detail.split(' · ').slice(1).join(' · '); // hole 2.4.0 · Docker 29.8 · 7 projects …
        box.classList.add('su-' + sm.tone);
        const toggle = () => { passedOpen = !passedOpen; draw(); };
        box.append(h('div', { class: 'su-ready-head' }, busy ? h('span', { class: 'spinner', title: busy === 'deep' ? 'mount test…' : 'checking…' }) : h('span', { class: 'su-dot', 'aria-hidden': 'true' }), h('b', {}, sm.title), h('span', { class: 'mut small grow' }, busy === 'deep' ? 'running the mount test (a few seconds)…'
            : sm.blockers.length ? 'checks again by itself once you fix it' : facts), h('div', { class: 'su-ready-acts' }, !sm.blockers.length && !sm.optional.length ? h('button', { class: 'ghost', 'aria-expanded': String(passedOpen), onclick: toggle }, passedOpen ? 'Hide ‹' : 'Details ›') : null, h('button', { class: 'ghost', disabled: !!busy, 'data-fk': 'check', onclick: () => void check(false, true) }, 'Check again'))));
        if (sm.blockers.length)
            box.append(h('ul', { class: 'su-rows' }, ...sm.blockers.map(blockerEl)));
        if (sm.blockers.length || sm.optional.length) {
            box.append(h('button', { class: 'su-fold', 'aria-expanded': String(passedOpen), onclick: toggle }, h('span', { class: 'su-chev', 'aria-hidden': 'true' }, passedOpen ? '⌄' : '›'), sm.optional.length ? h('span', { class: 'su-dot su-warn', 'aria-hidden': 'true' }) : null, h('span', { class: 'grow' }, sm.fold), h('span', { class: 'mut small' }, `${sm.passed} passed`)));
        }
        if (passedOpen) {
            if (sm.optional.length)
                box.append(h('ul', { class: 'su-rows' }, ...sm.optional.map(rowEl)));
            box.append(h('ul', { class: 'su-passed' }, ...splitRows(rep).passed.map((r) => h('li', { title: r.text }, h('span', { class: 'ok' }, '✓ '), r.title))));
            if (!rep.deep)
                box.append(h('div', { class: 'su-deep small' }, h('span', { class: 'mut grow' }, 'Mount test: a short container with a nested directory mount, looked at after 3 s (K18).'), h('button', { class: 'mini', disabled: !!busy, 'data-fk': 'deep', onclick: () => void check(true) }, 'Run the mount test')));
        }
        return box;
    }
    /** 9.54: hole's agent image - information; the rebuild is armed in ⌘K (F25: "Rebuild: ⌘K" at the row's end) */
    function imageEl() {
        const rb = deps.rebuild();
        const ln = imageLine(img, rb.pending, rb.by);
        const flip = async (on) => { await deps.setRebuild(on); draw(); };
        return h('section', { class: 'su-image' + (rb.pending ? ' armed' : '') }, h('div', { class: 'su-image-row' }, h('span', { class: 'su-dot su-' + ln.tone, 'aria-hidden': 'true' }), h('b', {}, 'Sandbox image'), h('span', { class: 'small grow ' + (ln.tone === 'warn' ? 'warn' : 'mut') }, ln.text), ln.action ? h('button', { class: 'mini', 'data-fk': 'rebuild', title: 'the next start runs without --rebuild',
            onclick: () => void flip(false) }, 'Cancel')
            : rb.pending ? null : h('span', { class: 'su-image-hint mut small', title: REBUILD_HINT + ' ' + rebuildWhere() }, kbd('Rebuild: ⌘K'))), rb.pending ? h('p', { class: 'su-image-note warn small' }, rebuildNote(rb.by)) : null);
    }
    function presetsEl(f) {
        const cards = f.presets.map((p) => presetCard(f, p, f.preset === p[0], () => void save({ preset: p[0] })));
        return h('section', { class: 'su-sec' }, h('h3', {}, 'Pick your Piper'), h('p', { class: 'mut small' }, 'Every preset keeps the core: sessions, terminal, Files, Changes and the isolation. Switch any module later in Settings → Features.', f.preset === 'custom' ? h('b', { class: 'su-custom' }, ' Now: your own mix of modules - a preset starts over.') : null), h('div', { class: 'su-presets', role: 'radiogroup', 'aria-label': 'preset' }, ...cards));
    }
    /** F25 step 2: the presets as a segment (as Settings › Features, F19), what the pick holds, the gallery behind a link */
    function pickEl(f) {
        const diff = presetDiff(f, f.modules);
        const now = f.preset;
        const seg = h('div', { class: 'fm-seg', role: 'radiogroup', 'aria-label': 'preset' }, ...f.presets.map(([id, label, text]) => h('button', { type: 'button', role: 'radio', class: now === id ? 'on' : '', 'aria-checked': String(now === id),
            'data-fk': `preset:${id}`, title: text, onclick: () => void save({ preset: id }) }, label)), now === 'custom' ? h('span', { class: 'fm-custom on', role: 'radio', 'aria-checked': 'true', 'aria-disabled': 'true' }, 'Custom') : null);
        const chip = (x) => h('span', { class: 'fm-chip' }, x);
        const line = h('div', { class: 'fm-line small' }, ...(now === 'custom' && diff.label
            ? [h('span', { class: 'mut' }, `Custom = ${diff.label}`), ...(diff.added.length ? [h('span', { class: 'mut' }, ' +'), ...diff.added.map(chip)] : []),
                ...(diff.removed.length ? [h('span', { class: 'mut' }, ' − ' + diff.removed.join(', '))] : [])]
            : [...presetChips(f, now).map(chip)]));
        const text = f.presets.find(([id]) => id === now)?.[2];
        return h('section', { class: 'su-pick' }, h('p', { class: 'mut small' }, 'Every preset keeps the core: sessions, terminal, Files, Changes and the isolation. Switch any module later in Settings → Features.'), seg, text ? h('p', { class: 'small' }, text) : null, line, h('button', { class: 'link small', 'aria-expanded': String(galleryOpen), 'data-fk': 'gallery', onclick: () => { galleryOpen = !galleryOpen; draw(); } }, galleryOpen ? 'Hide the modules ‹' : 'What each module adds ›'), galleryOpen ? galleryEl(f) : null);
    }
    /** a sketch per module (img/modules/<id>.svg, dummy data); off = dimmed; a module's own check sits on its tile */
    function galleryEl(f) {
        const byModule = new Map();
        for (const r of rep ? splitRows(rep).info : []) {
            const m = MODULE_ROW[r.key];
            if (m)
                byModule.set(m, r);
        }
        const tiles = f.list.filter(([id]) => presetModules(f).includes(id)).map(([id, label, text]) => {
            const on = f.modules[id] !== false;
            const problem = on ? byModule.get(id) : undefined;
            return moduleTile(id, label, text, on, () => void save({ modules: { [id]: !on } }), {
                cls: problem ? 'su-tile-' + problem.level : '',
                note: problem ? h('div', { class: 'su-tile-note small' }, h('span', { class: 'su-mark' }, MARK[problem.level]), h('span', { class: 'grow' }, problem.title), problem.settings ? h('button', { class: 'mini', onclick: () => deps.openSettings(problem.settings) }, 'Settings') : null, actionBtn(problem)) : null
            });
        });
        return h('section', { class: 'su-sec' }, h('h3', {}, 'What each module adds'), h('p', { class: 'mut small' }, 'A dimmed module is off in the preset you picked - flip the switch to have it anyway. Your data and settings stay either way.'), h('div', { class: 'su-gallery' }, ...tiles));
    }
    /** F25 step 3: how the first session goes */
    function firstEl() {
        return h('section', { class: 'su-first' }, h('ol', { class: 'su-steps' }, ...STEPS.map(([t, d]) => h('li', {}, h('b', {}, t), h('span', { class: 'mut small' }, d)))));
    }
    /** F25: the welcome's footer - a blocked check keeps "continue anyway", smaller */
    function footEl(fails) {
        const later = h('button', { class: 'link', onclick: () => void finish(false) }, 'Later – just close');
        const next = (label) => h('button', { class: fails ? '' : 'primary', 'data-fk': 'next', onclick: () => { step += 1; draw(); } }, label);
        const back = step > 1 ? h('button', { class: 'ghost', onclick: () => { step -= 1; draw(); } }, '‹ Back') : null;
        const note = fails ? h('span', { class: 'mut small grow' }, `Fix the blocker${fails === 1 ? '' : 's'} first — or continue anyway`) : h('span', { class: 'grow' });
        if (step === 1)
            return h('div', { class: 'su-foot' }, note, later, next(fails ? 'Continue anyway' : 'Next: pick modules'));
        if (step === 2)
            return h('div', { class: 'su-foot' }, back, note, later, next('Next: start a session'));
        return h('div', { class: 'su-foot' }, back, note, fails ? h('button', { class: 'link', onclick: () => void finish(false) }, 'Continue anyway') : later, fails ? h('button', { disabled: true, title: 'a check above blocks sessions' }, `Fix the blocker${fails === 1 ? '' : 's'} first`)
            : h('button', { class: 'primary', onclick: () => void finish(true) }, 'Start first session'));
    }
    function draw() {
        const top = el.scrollTop; // a click redraws the card: stay where the user scrolled
        // N10-D-5: ... and on the control the keyboard was on (a switch / preset / Check again: focus fell to <body>)
        //   a button disabled while the check runs takes it back on the redraw after the check
        const fk = (el.contains(document.activeElement) ? document.activeElement.dataset.fk : undefined)
            ?? (document.activeElement === document.body ? keepFk : undefined);
        clear(el);
        const f = deps.features();
        const welcome = !!f && !f.onboarded;
        const fails = rep?.fail ?? 0;
        el.classList.toggle('welcome', welcome);
        if (welcome) {
            // F25: one card - the hero tiles, the stepper, the step, the footer
            el.append(h('div', { class: 'su-wiz' }, heroEl(), stepperEl(), h('div', { class: 'su-wiz-body' }, ...(step === 1 ? [checkEl(), rep && hole() ? imageEl() : null] : step === 2 ? [pickEl(f)] : [firstEl()])), footEl(fails)));
        }
        else {
            el.append(h('h1', {}, 'Installation check & features')); // hidden in the Setup view, which has its own header (app.css)
            el.append(checkEl());
            if (rep && hole())
                el.append(imageEl());
            if (f)
                el.append(presetsEl(f), galleryEl(f));
            el.append(h('div', { class: 'row su-done' }, fails ? h('span', { class: 'warn small grow' }, `${fails} ${fails === 1 ? 'check blocks' : 'checks block'} sessions - fix ${fails === 1 ? 'it' : 'them'} first, or continue anyway.`) : h('span', { class: 'grow' }), h('button', { class: 'link', 'data-fk': 'features', onclick: () => deps.openSettings('features') }, 'Modules: Settings › Features'), h('button', { class: 'primary', onclick: () => void finish(false) }, 'Close')));
        }
        el.scrollTop = top;
        const back = fk ? el.querySelector(`[data-fk="${CSS.escape(fk)}"]`) : null;
        if (back && !back.disabled)
            back.focus({ preventScroll: true });
        keepFk = back?.disabled ? fk ?? '' : '';
    }
    return {
        el,
        /** the last report (null until the first check) */
        report: () => rep,
        /** shown on its own (the empty main area) or on demand (Settings, palette) */
        wanted: (st) => !dismissed && setupWanted(st, rep),
        open() {
            dismissed = false;
            draw();
            if (!rep && !busy)
                void check();
            else
                queueMicrotask(schedule); // on screen after the caller mounts it
        },
        /** the first check in the background (the poll decides whether the card shows) */
        prime() {
            if (!rep && !busy)
                void check();
        },
        redraw() {
            draw();
            schedule();
        },
        dismiss() {
            dismissed = true;
            unschedule();
        },
    };
}
//# sourceMappingURL=setup.js.map