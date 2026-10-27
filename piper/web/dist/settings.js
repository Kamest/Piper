import { createServerSettings } from './environments.js';
import { createMobileSettings } from './mobilesettings.js';
import { apiFetch } from './api.js';
// Settings: one page over policy.json, projects.json, the
// GitLab token and the read-only environment. piperd (settings.py) owns the schema, the
// validation and the weakening check; this page renders the fields, sends one section at
// a time and asks for the confirmation piperd demands (409 needs_confirm).
import { token } from './api.js';
import { kbd } from './clipkeys.js';
import { clear, fi, h } from './dom.js';
import { moduleRow, presetChips, presetDiff } from './setup.js';
import { MM_AFTER, MM_DETAIL, mmAdvancedSummary, mmStatusLine } from './mm.js';
import { markBidi, matchRanges } from './text.js';
function readStore(k) {
    try {
        return localStorage.getItem(k);
    }
    catch {
        return null;
    }
}
function writeStore(k, v) {
    try {
        localStorage.setItem(k, v);
    }
    catch { /* private window: not remembered */ }
}
/** every switch of a group (Approvals: all its sub-groups) */
export function secItems(g) {
    return [...(g.items ?? []), ...(g.groups ?? []).flatMap((x) => x.items)];
}
/** Hide disabled feature areas without changing any of their saved values. */
export function secVisibleTree(tree, github = true) {
    return github ? tree : tree.map(g => g.groups ? { ...g, groups: g.groups.filter(sg => sg.id !== 'github') } : g);
}
/** how many of the items are on: [on, all] */
export function secCount(items, v) {
    return [items.filter((i) => v[i.id] === true).length, items.length];
}
/** Apply a selection without ever switching off mandatory approvals. */
export function secSetItems(v, items, on) {
    const next = { ...v };
    for (const i of items)
        next[i.id] = i.required ? true : on;
    return next;
}
/** Count changed switches rather than the single object containing the entire Security section. */
export function secChangedCount(tree, before, after) {
    return tree.flatMap(secItems).filter(i => (before[i.id] === true) !== (after[i.id] === true)).length;
}
/** whether a saved state exactly matches a preset across the full tree */
export function secMatchesPreset(v, preset, ids) {
    return ids.every((id) => (v[id] === true) === (preset[id] === true));
}
/** the summary's name for a state: the selected default preset, all on, or a custom mix */
export function secStateName(on, all, plainHole) {
    return plainHole ? 'Minimal' : on === 0 ? 'Plain hole' : on === all ? 'Strict' : 'Custom';
}
/** the ids whose label, help, risk or card kind contain q (a group or sub-group title = all its items) */
export function secMatches(tree, q) {
    const needle = q.trim().toLowerCase();
    const out = new Set();
    if (!needle)
        return out;
    const has = (x) => String(x ?? '').toLowerCase().includes(needle);
    for (const g of tree) {
        const all = has(g.title);
        for (const i of g.items ?? [])
            if (all || [i.label, i.help, i.risk].some(has))
                out.add(i.id);
        for (const sg of g.groups ?? [])
            for (const i of sg.items)
                if (all || has(sg.title) || [i.label, i.kind].some(has))
                    out.add(i.id);
    }
    return out;
}
/** "… before the start (L1)." -> the help without the layer code, and the code as a tag */
export function secTag(help) {
    const m = /\s*\(((?:L|K)\d+)\)/.exec(help);
    return m ? { help: help.slice(0, m.index) + help.slice(m.index + m[0].length), tag: m[1] } : { help, tag: '' };
}
/** F26: the order of the "Claude asks you before" rows; a group piperd adds later goes last */
const SEC_ASK_ORDER = ['git', 'gitlab', 'github', 'projects', 'db', 'http', 'agents'];
/** F26: the three levels as rows - what each one means for the user, in one sentence (security.py presets) */
export const SEC_LEVELS = [
    ['hole', 'Minimal', "Keeps shared agent homes and service tokens. Protects host, Piper and hidden files; asks before prod DB/HTTP, unknown HTTP and privileged actions."],
    ['recommended', 'Recommended', "Routine edit, build, test, commit and fetch run free. Shared homes, secrets stripped (GH_TOKEN kept), DB masked; asks before push, merge, CI and spawn."],
    ['guarded', 'Strict', "Hole only with a verified sandbox: private homes, filtered network, no service tokens. Anything outside the sandbox needs approval; prod DB writes, merge and CI denied."],
];
/** A profile's extra rules require explicit selection; identical saved switches can remain Custom. */
export function secSelectedProfile(value, saved, profile, presets, ids) {
    return profile && profile !== 'custom' && presets[profile] && secMatchesPreset(value, saved, ids)
        && secMatchesPreset(value, presets[profile], ids) ? profile : undefined;
}
/** F26: the level closest to a state (fewest differing switches; a tie goes to Recommended, then Minimal) and
 *  the ids that differ - "Recommended + 1 change" instead of a fourth level */
export function secNearest(v, presets, ids) {
    let best = null;
    for (const id of ['recommended', 'hole', 'guarded']) {
        const p = presets[id];
        if (!p)
            continue;
        const changed = ids.filter((i) => (v[i] === true) !== (p[i] === true));
        if (!best || changed.length < best.changed.length)
            best = { id, label: SEC_LEVELS.find((l) => l[0] === id)[1], changed };
    }
    return best ?? { id: '', label: 'Custom', changed: [] };
}
/** F26: the level's name for the nav and the tree - "Recommended", "Recommended + 2 changes", "Plain hole" */
export function secLevelName(v, presets, ids) {
    if (ids.length && (presets.plain ? secMatchesPreset(v, presets.plain, ids) : ids.every((i) => v[i] !== true)))
        return 'Plain hole';
    const n = secNearest(v, presets, ids);
    return n.changed.length ? `${n.label} + ${n.changed.length} change${n.changed.length === 1 ? '' : 's'}` : n.label;
}
/** the short phrases of these items, each once, in order */
function shorts(items) {
    return [...new Set(items.map((i) => i.short || secKindLabel(i).toLowerCase()))];
}
/** F26: one ask group in words - what Claude asks before (switch on) and what it does on its own (off, still audited);
 *  generated from the current choice, never fixed text */
export function secAskSentence(items, v) {
    const on = shorts(items.filter((i) => v[i.id] === true));
    const off = shorts(items.filter((i) => v[i.id] !== true)).filter((x) => !on.includes(x)); // a shared phrase asks if any kind asks
    return { asks: on.length ? on.join(', ') : 'nothing', own: off.length ? off.join(', ') : '' };
}
/** F26: "Always protected: …" - the guards that are on and never ask (the tree items), each phrase once */
export function secAlways(tree, v) {
    return shorts(tree.flatMap((g) => g.items ?? []).filter((i) => v[i.id] === true));
}
/** the rows the projects table shows for a search text and a filter (indexes into the full list) */
export function filterProjects(rows, q, filter, suggestions = {}, ignored = []) {
    const needle = q.trim().toLowerCase();
    const gl = (r) => (Array.isArray(r.gitlab) ? r.gitlab.join(' ') : String(r.gitlab ?? ''));
    const out = [];
    rows.forEach((r, i) => {
        if (needle && ![r.key, r.path, gl(r), r.obs, r.title].some((x) => String(x ?? '').toLowerCase().includes(needle)))
            return;
        if (filter === 'gitlab' && !gl(r).trim())
            return;
        if (filter === 'obs' && !r.obs)
            return;
        if (filter === 'explicit' && !r.explicit)
            return;
        if (filter === 'suggested' && !(r.key in suggestions && !r.obs))
            return;
        if (filter === 'unmapped' && (r.obs || ignored.includes(r.key)))
            return;
        out.push(i);
    });
    return out;
}
/** the page-wide search: section id -> keys of its matching fields (a matching section title = all of them); null = no search */
/** W14: the tree path over a section, "Version Control › Git"; first = the group's first
 * section (the group part of the crumb opens it). Titles come from piperd's GROUPS (an unknown id shows as is). */
/** N14: the open section's nav item scrolled into the nav (not the page) - a section deep in "Advanced &
 *  isolation" sat below the nav's edge; the nav keeps its own scroll across redraws otherwise (N10: sideways too) */
export function revealNavItem(nav) {
    const on = nav.querySelector('.st-navi.on');
    if (!on)
        return;
    const box = nav.getBoundingClientRect(), at = on.getBoundingClientRect();
    if (at.top < box.top)
        nav.scrollTop -= box.top - at.top + 8;
    else if (at.bottom > box.bottom)
        nav.scrollTop += at.bottom - box.bottom + 8;
    // N10: ≤ 640 px the nav is one row that scrolls sideways (app.css)
    if (at.left < box.left)
        nav.scrollLeft -= box.left - at.left + 8;
    else if (at.right > box.right)
        nav.scrollLeft += at.right - box.right + 8;
}
/** N11: the "applies after …" most rows of a section share - said once under the title, only the rows that
 *  differ keep their own line; `all` = every row (the old rule), '' = no text two rows share */
export function sharedApply(fields) {
    const n = new Map();
    for (const f of fields)
        if (f.apply !== 'now' && f.apply_text)
            n.set(f.apply_text, (n.get(f.apply_text) ?? 0) + 1);
    const [text, count] = [...n].sort((a, b) => b[1] - a[1])[0] ?? ['', 0];
    if (count < 2)
        return { text: '', all: false };
    return { text, all: fields.every((f) => f.apply !== 'now' && f.apply_text === text) };
}
export function settingsCrumb(groups, sections, sid) {
    const s = sections.find((x) => x.id === sid);
    if (!s)
        return null;
    const g = s.group ?? 'general';
    return { group: groups.find(([id]) => id === g)?.[1] ?? g, title: s.title,
        first: sections.find((x) => (x.group ?? 'general') === g).id };
}
export function searchSettings(sections, q, github = true) {
    const needle = q.trim().toLowerCase();
    if (!needle)
        return null;
    const has = (x) => String(x ?? '').toLowerCase().includes(needle);
    const out = new Map();
    for (const s of sections) {
        const all = has(s.title) || (s.id === 'mobile' && has('phone pair QR mobile access'));
        const keys = s.fields.filter((f) => all || [f.label, f.help, settingsFieldCopy(f).label, settingsFieldCopy(f).summary, f.key, f.env, ...(f.choices ?? []).flatMap((c) => [c[1], c[0]])].some(has)
            || (f.type === 'security' && secMatches(secVisibleTree(f.tree ?? [], github), q).size > 0)).map((f) => f.key);
        if (keys.length || all)
            out.set(s.id, keys);
    }
    return out;
}
/** text with every (case-insensitive) occurrence of q in <mark> - no innerHTML */
function marked(text, q) {
    const ql = q.trim().toLowerCase();
    if (!ql)
        return [markBidi(text)];
    const out = [];
    let at = 0;
    for (const [i, j] of matchRanges(text, q.trim())) {
        out.push(markBidi(text.slice(at, i)), h('mark', {}, text.slice(i, j)));
        at = j;
    }
    out.push(markBidi(text.slice(at)));
    return out;
}
function searchIcon() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'st-sic');
    for (const d of ['M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14z', 'M20 20l-4-4']) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
const SHIELD = 'M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z';
const BOX = ['M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z', 'm3.3 7 8.7 5 8.7-5', 'M12 22V12'];
const RING = 'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20z';
/** 9.46: the few lucide shapes the Security page draws (no icon font in the GUI) */
const SEC_ICONS = {
    shield: [SHIELD], 'sliders-horizontal': ['M21 4H10', 'M6 4H3', 'M21 12H18', 'M14 12H3', 'M21 20H10', 'M6 20H3', 'M10 2v4', 'M18 10v4', 'M10 18v4'],
    'shield-half': [SHIELD, 'M12 22V2'], 'shield-check': [SHIELD, 'm9 12 2 2 4-4'], box: BOX,
    'shield-off': ['m2 2 20 20', 'M5 5a1 1 0 0 0-1 1v7c0 5 3.5 7.5 7.67 8.94a1 1 0 0 0 .67.01c2.35-.82 4.47-1.97 5.86-3.53',
        'M9.309 3.652A12.252 12.252 0 0 0 11.24 2.28a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1v7a9.784 9.784 0 0 1-.08 1.264'],
    start: ['M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z',
        'm12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z', 'M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0', 'M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5'],
    sandbox: [...BOX, 'm7.5 4.27 9 5.15'],
    tools: ['M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z'],
    approvals: ['M5 22h14', 'M19.27 13.73A2.5 2.5 0 0 0 17.5 13h-11A2.5 2.5 0 0 0 4 15.5V17a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-1.5c0-.66-.26-1.3-.73-1.77Z',
        'M14 13V8.5C14 7 15 7 15 5a3 3 0 0 0-3-3c-1.66 0-3 1-3 3s1 2 1 3.5V13'],
    chev: ['m6 9 6 6 6-6'], risk: ['m15 10 5 5-5 5', 'M4 4v7a4 4 0 0 0 4 4h12'],
    'next start': ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
    'at once': ['M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z'],
    info: [RING, 'M12 16v-4', 'M12 8h.01'],
    git: ['M6 3v12', 'M18 3a3 3 0 1 0 0 6a3 3 0 1 0 0-6z', 'M6 15a3 3 0 1 0 0 6a3 3 0 1 0 0-6z', 'M18 9a9 9 0 0 1-9 9'],
    gitlab: ['m22 13.29-3.33-10a.42.42 0 0 0-.14-.18.38.38 0 0 0-.22-.11.39.39 0 0 0-.23.07.42.42 0 0 0-.14.18l-2.26 6.67H8.32L6.1 3.26a.42.42 0 0 0-.1-.18.38.38 0 0 0-.26-.08.39.39 0 0 0-.23.07.42.42 0 0 0-.14.18L2 13.29a.74.74 0 0 0 .27.83L12 21l9.69-6.88a.71.71 0 0 0 .31-.83Z'],
    github: ['M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4', 'M9 18c-4.51 2-5-2-7-2'],
    projects: ['M20 17a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.9a2 2 0 0 1-1.69-.9l-.81-1.2a2 2 0 0 0-1.67-.9H8a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2Z', 'M2 8v11a2 2 0 0 0 2 2h14'],
    http: [RING, 'M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20', 'M2 12h20'],
    db: ['M3 5a9 3 0 1 0 18 0a9 3 0 1 0-18 0', 'M3 5v14a9 3 0 0 0 18 0V5', 'M3 12a9 3 0 0 0 18 0'],
    agents: ['M12 8V4H8', 'M6 8h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2z', 'M2 14h2', 'M20 14h2', 'M15 13v2', 'M9 13v2'],
};
/** an Approvals row's label without the "(git_push)" the card labels carry: the kind is on the line under it */
export function secKindLabel(i) {
    return i.kind ? i.label.replace(` (${i.kind})`, '') : i.label;
}
/** the Approvals cards in four columns as on the board: the short ones share a column */
const SEC_COLS = [['git', 'projects'], ['gitlab'], ['github', 'http'], ['db', 'agents']];
export function secColumns(subs) {
    const cols = SEC_COLS.map((ids) => subs.filter((x) => ids.includes(x.id)));
    for (const x of subs)
        if (!SEC_COLS.flat().includes(x.id))
            cols[3].push(x); // a kind group piperd adds later
    return cols.filter((c) => c.length);
}
function secIcon(name, cls = 'sc-ic') {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', cls);
    for (const d of SEC_ICONS[name] ?? []) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
// ------------------------------------------------------------------ pure helpers (node tests)
/** a textarea (one item per line) -> the list piperd validates; ports as numbers */
export function parseList(text, asInt = false) {
    const out = [];
    for (const raw of text.split('\n')) {
        const x = raw.trim();
        if (!x)
            continue;
        const v = asInt && /^\d+$/.test(x) ? Number(x) : x;
        if (!out.includes(v))
            out.push(v);
    }
    return out;
}
export function listText(v) {
    return Array.isArray(v) ? v.map(String).join('\n') : '';
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
/** Presentation only: groups never change field keys, defaults or save boundaries. Unknown fields stay visible. */
const FIELD_GROUPS = {
    security: [['', ['security']], ['Databases', ['db_dry_run']]],
    interface: [['Appearance', ['theme']], ['Navigation', ['nav_history', 'restore_view']], ['Terminal links', ['host_link_browser', 'term_click_opens_files']]],
    sessions: [['General', ['max_sessions', 'language']], ['Tools', ['maven_settings', 'idea_in_sessions', 'idea_mcp_url', 'codex_in_claude']], ['Access & approvals', ['default_unlocks', 'auto_approve', 'write_guard']], ['Terminal access', ['open_links_on_host', 'clipboard_arm_enter']]],
    sandbox: [['Agent configuration', ['claude_dir', 'codex_dir', 'login_spread']], ['Network & projects', ['unrestricted_network', 'stack_scope_project', 'worktree_stack']], ['Protection checks', ['settings_auto_tighten', 'l5_action']]],
    git: [['Git behavior', ['git_mode', 'git_host_hint', 'commit_requires_review']], ['Protected branches', ['protected_branches', 'mr_target_protected']], ['Host access', ['git_fail_closed', 'git_run_projects', 'git_run_readonly_extra', 'git_hidden_server_content', 'git_run_output_in_repo']]],
    gitlab: [['Notifications', ['gitlab_notifications']], ['Connection', ['gitlab_host', 'token']], ['Agent access', ['gitlab_read_always', 'gitlab_extra_tools']]],
    databases: [['Your database console', ['gui_timeout_s', 'console_optimizer_hints', 'ssh_user', 'tunnel_idle_min']], ['Agent query limits', ['max_rows', 'max_bytes', 'max_cell', 'statement_timeout_s', 'max_timeout_s']], ['Production & personal data', ['prod_read', 'prod_require_where', 'mask_test', 'mask_words']]],
    scan: [['Files & exceptions', ['candidates', 'allow', 'skip_dirs', 'scan_skip_roots']], ['Scan limits', ['max_depth', 'symlink_visits', 'max_entries']], ['Configuration & environment', ['forbidden_project_keys', 'forbidden_gateway_ports', 'secret_env', 'allowed_env']]],
    holehome: [['Files & libraries', ['strip_includes', 'rw_includes', 'rw_libraries', 'force_exclude']], ['Environment variables', ['strip_env', 'strip_env_values']], ['Network', ['strip_allow', 'strip_ports']]],
    env: [['Server', ['piper_home', 'project_path', 'port', 'bind']], ['Tools & project data', ['hole_bin', 'docker_bin']], ['Intervals & limits', ['watch_interval', 'pty_buffer', 'mm_interval', 'prod_tick', 'prod_timeout']]],
};
export function settingsFieldGroups(sid, fields) {
    const remaining = new Set(fields);
    const result = (FIELD_GROUPS[sid] ?? []).map(([title, keys]) => ({ title, fields: keys.flatMap((key) => {
            const f = fields.find((item) => item.key === key);
            if (!f)
                return [];
            remaining.delete(f);
            return [f];
        }) })).filter((g) => g.fields.length);
    if (remaining.size)
        result.push({ title: result.length ? 'Other settings' : '', fields: [...remaining] });
    return result;
}
const FIELD_COPY = {
    max_sessions: ['Concurrent sessions', 'Maximum number of active sessions. Blocked and ended sessions do not count.'],
    language: ['Response language', 'The language agents use to answer you. Code and commits follow the project.'],
    maven_settings: ['Maven configuration', 'Generate session settings from the active profiles in your host Maven configuration.'],
    idea_in_sessions: ['IntelliJ tools in sessions', 'Give agents access to your local IDE. You can also enable this for a single session.'],
    idea_mcp_url: ['IntelliJ connection URL', 'Leave empty to use the default local IntelliJ connection.'],
    codex_in_claude: ['Codex as a Claude subagent', 'Give Claude sessions access to the configured Codex login and tools.'],
    default_unlocks: ['Default session permissions', 'Permissions unlocked when a new session starts. You can lock them again in its Isolation panel.'],
    nav_history: ['Back and forward navigation', 'Use mouse side buttons or ⌘[ and ⌘] to revisit sessions, views and files.'],
    restore_view: ['Restore the last view', 'After reloading Piper, return to the session and view you had open.'],
    term_click_opens_files: ['Open terminal file links in Piper', '⌘/Ctrl+click opens Files; Shift+click opens IntelliJ. When off, both shortcuts open IntelliJ.'],
    open_links_on_host: ['Open web links on your computer', 'Open links from session terminals in the host browser.'],
    clipboard_arm_enter: ['Allow terminal clipboard writes after Enter', 'After Enter or a drag outside the terminal, allow a session to copy text to your clipboard.'],
    claude_dir: ['Claude configuration', 'Choose an isolated configuration or share your host login, plugins and conversation history.'],
    codex_dir: ['Codex configuration', 'Choose an isolated configuration or share the host login, settings and writable conversation history.'],
    login_spread: ['Share refreshed logins', 'Copy a login refreshed in one sandbox to the other running sessions.'],
    settings_auto_tighten: ['Accept more restrictive sandbox changes', 'Skip approval when a project sandbox change only adds file exclusions. See details for network conditions.'],
    l5_action: ['When a sensitive file appears', 'Flag the session for your attention, or stop it.'],
    git_fail_closed: ['Restrict Git running on the host', 'Disable Git hooks and refuse repository configuration that could run programs on your computer.'],
    git_hidden_server_content: ['Allow hidden files already on GitLab', 'Allow branch operations without a card when hidden-file contents are verified on GitLab. The sandbox may then read those files.'],
    git_run_readonly_extra: ['Allow additional read-only Git commands', 'Let agents search Git history and references without an approval card.'],
    git_run_output_in_repo: ['Allow Git output files in the repository', 'Let host Git write archive output inside the repository without an approval card.'],
    protected_branches: ['Protected branches', 'Branch names for the push/merge protection and protected CI-job approval switches in Security.'],
    mr_target_protected: ['Blocked merge request targets', 'Branches that a merge request may not target.'],
    max_rows: ['Maximum result rows', 'Maximum number of rows returned to an agent in one call.'],
    max_bytes: ['Maximum result size (bytes)', 'Limit the total result returned to an agent.'],
    max_cell: ['Maximum cell size (bytes)', 'Limit how much of one database cell an agent receives.'],
    statement_timeout_s: ['Default agent query timeout (s)', 'Time allowed for an agent query unless it requests a different timeout.'],
    max_timeout_s: ['Maximum agent query timeout (s)', 'The longest timeout an agent may request.'],
    gui_timeout_s: ['Console query timeout (s)', 'Time allowed for queries you run in the Database view.'],
};
/** Keep the full source explanation available; never truncate a warning or split a path/URL. */
export function settingsFieldCopy(f) {
    const copy = FIELD_COPY[f.key];
    const help = f.help ?? '';
    const first = help.match(/^(.+?\.)\s+(?=[A-Z])/s)?.[1];
    const summary = copy?.[1] ?? (first && first.length <= 200 ? first : help);
    return { label: copy?.[0] ?? f.label, summary, detail: summary !== help ? help : '' };
}
/** keys whose edited value differs from what the page loaded (a secret: any typed value) */
export function changedKeys(orig, cur) {
    return Object.keys(cur).filter((k) => !same(orig[k], cur[k]));
}
/** a quick client-side check; piperd validates again and has the final word */
export function localError(f, v) {
    if (f.type === 'int') {
        const n = Number(v);
        if (v === '' || !Number.isInteger(n))
            return 'a whole number';
        if ((f.min !== undefined && n < f.min) || (f.max !== undefined && n > f.max))
            return `range ${f.min}–${f.max}`;
    }
    if (f.type === 'time' && !(f.default === '' && v === '') && !/^([01]\d|2[0-3]):[0-5]\d$/.test(String(v)))
        return 'time HH:MM';
    if (f.type === 'secret' && typeof v === 'string' && /\s/.test(v.trim()))
        return 'no spaces';
    return '';
}
/** "takes effect immediately" etc. for the saved keys, deduplicated */
export function applyNote(texts) {
    return [...new Set(texts)].join(' · ');
}
/** never throws: a network error is status 0 with the message in json.error */
async function req(method, path, body) {
    const headers = { Authorization: 'Bearer ' + token() };
    const init = { method, headers, cache: 'no-store' };
    if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
    }
    try {
        const r = await apiFetch(path, init);
        let json = {};
        try {
            json = (await r.json());
        }
        catch { /* empty */ }
        return { status: r.status, json };
    }
    catch (e) {
        return { status: 0, json: { error: String(e instanceof Error ? e.message : e) } };
    }
}
export function createSettings(deps) {
    const el = h('div', { class: 'settings' });
    const servers = createServerSettings({ changed: () => deps.changed?.(), toast: deps.toast, modal: deps.modal, closeModal: deps.closeModal });
    const mobile = createMobileSettings({ toast: deps.toast, modal: deps.modal, closeModal: deps.closeModal });
    mobile.el.addEventListener('mobile-loaded', () => {
        const nav = el.querySelector('[data-nav="mobile"]');
        nav?.querySelector('.st-cnt')?.remove();
        if (mobile.count())
            nav?.append(h('span', { class: 'st-cnt' }, String(mobile.count())));
    });
    function serverNav() {
        const nav = el.querySelector('[data-nav="servers"]');
        if (!nav)
            return;
        nav.classList.toggle('dirty', servers.dirty());
        if (query)
            return;
        nav.querySelector('.st-cnt')?.remove();
        const count = navCount({ id: 'servers' });
        if (count)
            nav.append(count);
    }
    servers.el.addEventListener('server-dirty', serverNav);
    servers.el.addEventListener('servers-loaded', serverNav);
    let sections = [];
    /** [id, title] in tree order, from GET /api/settings (W14) */
    let groups = [];
    let loading = false;
    let loadErr = '';
    // per section: loaded values, edited values, secrets to clear, keys to reset, field errors
    const orig = new Map();
    const cur = new Map();
    const clears = new Map();
    const resets = new Map();
    const errs = new Map();
    /** keys whose error came from piperd (a section-wide check may pin it to another field): cleared on any edit */
    const srvErrs = new Map();
    const notes = new Map();
    let projSearch = ''; // the projects table's search text (kept across redraws)
    let query = ''; // the page-wide search (kept while the page lives, not across reloads)
    let secSearchAll = false;
    let showAll = false; // the selected section shows all its fields despite the search
    const search = h('input', { type: 'search', class: 'st-search', placeholder: 'Search settings', autocomplete: 'off', spellcheck: 'false',
        'aria-label': 'search the settings' });
    search.addEventListener('input', () => { query = search.value; showAll = secSearchAll = false; draw(); });
    search.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !search.value)
            return;
        e.stopPropagation();
        search.value = query = '';
        draw();
    });
    /** keep: after saving one section, the unsaved edits of the others survive the reload */
    async function load(keep = false) {
        loading = true;
        mmAsked = false; // F22: a save reloads - the status line asks again
        draw();
        const r = await req('GET', '/api/settings');
        loading = false;
        if (r.status !== 200) {
            loadErr = String(r.json.error ?? `HTTP ${r.status}`);
        }
        else {
            loadErr = '';
            const before = new Map(sections.map((s) => [s.id, s]));
            sections = r.json.sections ?? [];
            const sessionsAt = sections.findIndex(s => s.id === 'new_sessions');
            sections.splice(sessionsAt < 0 ? sections.findIndex(s => s.group === 'sessions') + 1 : sessionsAt + 1, 0, { id: 'servers', title: 'Servers', group: 'sessions', help: 'Start sessions on your own servers over SSH. They appear in the same queue as the sessions on this computer.', fields: [], actions: [], rev: '', error: '' });
            sections.splice(sections.findIndex(s => s.id === 'servers') + 1, 0, { id: 'mobile', title: 'Mobile access', group: 'sessions', help: 'Use Piper from your phone. Each paired phone has full access to Piper on this computer.', fields: [], actions: [], rev: '', error: '' });
            groups = r.json.groups ?? [];
            for (const s of sections) {
                const old = before.get(s.id);
                if (!keep || !old || !dirtyOf(s.id)) {
                    reset(s);
                    continue;
                }
                // a draft: the edited keys stay as typed, the rest follows the file; its rev stays the one the edits
                // started from, so a change made meanwhile elsewhere still ends in a conflict instead of being overwritten
                const edited = changedKeys(orig.get(s.id) ?? {}, cur.get(s.id) ?? {});
                const draft = cur.get(s.id) ?? {};
                const keptClears = clears.get(s.id) ?? new Set();
                const keptResets = resets.get(s.id) ?? new Set();
                const keptErrs = errs.get(s.id) ?? {};
                reset(s);
                for (const k of edited)
                    cur.get(s.id)[k] = draft[k];
                clears.set(s.id, keptClears);
                resets.set(s.id, keptResets);
                errs.set(s.id, keptErrs);
                s.rev = old.rev;
            }
        }
        draw();
    }
    function reset(s) {
        const o = {};
        for (const f of s.fields) {
            if (f.readonly || f.type === 'env')
                continue;
            o[f.key] = f.type === 'secret' ? '' : structuredClone(f.value);
        }
        orig.set(s.id, o);
        cur.set(s.id, structuredClone(o));
        clears.set(s.id, new Set());
        resets.set(s.id, new Set());
        errs.set(s.id, {});
    }
    /** F19: how many settings of a section differ from the file (a changed key, a secret to delete, a key back to default) */
    function unsavedCount(sid) {
        if (sid === 'security') {
            const f = sections.find(s => s.id === sid)?.fields.find(f => f.type === 'security');
            if (f)
                return secChangedCount(f.tree ?? [], orig.get(sid)?.[f.key] ?? {}, cur.get(sid)?.[f.key] ?? {});
        }
        return new Set([...changedKeys(orig.get(sid) ?? {}, cur.get(sid) ?? {}), ...(clears.get(sid) ?? []), ...(resets.get(sid) ?? [])]).size;
    }
    // F19: ⌘S saves the open section while Settings is on screen (the editor and Database have their own ⌘S)
    document.addEventListener('keydown', (e) => {
        if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== 's' || !el.isConnected || !el.offsetParent)
            return;
        e.preventDefault();
        const btn = el.querySelector(`section[data-sid="${CSS.escape(selected)}"] button.st-save`);
        if (btn && !btn.disabled)
            btn.click();
    });
    function dirtyOf(sid) {
        return changedKeys(orig.get(sid) ?? {}, cur.get(sid) ?? {}).length > 0 || !!clears.get(sid)?.size || !!resets.get(sid)?.size;
    }
    function setVal(sid, key, v) {
        notes.delete(sid); // "Saved: ..." told about the last save, not this new draft
        cur.get(sid)[key] = v;
        resets.get(sid).delete(key);
        clears.get(sid).delete(key); // Delete, then a new secret typed in: the new one is meant, not the delete
        const f = sections.find((s) => s.id === sid)?.fields.find((x) => x.key === key);
        const e = f ? localError(f, v) : '';
        clearErr(sid, key);
        if (e)
            errs.get(sid)[key] = e;
        markSection(sid);
    }
    /** the key's error and piperd's section-wide ones go: any edit (or "default") may have fixed them */
    function clearErr(sid, key) {
        const m = errs.get(sid);
        for (const k of srvErrs.get(sid) ?? [])
            delete m[k];
        delete m[key];
        srvErrs.delete(sid);
    }
    /** N10-D-2: a save redraws the page (the Save button too): the keyboard goes to the saved field, else its section */
    function refocus(sid, key) {
        if (document.activeElement !== document.body && document.activeElement?.isConnected)
            return; // the user went on meanwhile
        (el.querySelector(`#st-${sid}-${key ?? ''}`) ?? el.querySelector(`[data-nav="${sid}"]`))?.focus({ preventScroll: true });
    }
    /** only the section's footer and the field's error change while typing (no full redraw = no lost focus) */
    function markSection(sid) {
        if (sid === 'servers') {
            serverNav();
            return;
        } // the custom server editor owns its draft/savebar
        const dirty = dirtyOf(sid);
        el.querySelector(`[data-nav="${sid}"]`)?.classList.toggle('dirty', dirty);
        const box = el.querySelector(`section[data-sid="${sid}"]`);
        if (!box)
            return;
        box.classList.toggle('dirty', dirty);
        const save = box.querySelector('button.st-save');
        if (save)
            save.disabled = !dirty || Object.keys(errs.get(sid) ?? {}).length > 0;
        const undo = box.querySelector('button.st-undo');
        if (undo)
            undo.disabled = !dirty;
        const bar = box.querySelector('.st-savebar');
        if (bar) {
            bar.hidden = !dirty;
            const n = unsavedCount(sid);
            bar.querySelector('.st-scount').textContent = markBidi(`${n} unsaved change${n === 1 ? '' : 's'}`);
            // The bar can become two lines on a phone. Its first appearance must not cover the field being edited.
            const active = document.activeElement;
            if (dirty && active instanceof HTMLElement && box.contains(active) && active.matches('input, textarea, select')) {
                const covered = active.getBoundingClientRect().bottom - bar.getBoundingClientRect().top;
                if (covered > 0)
                    box.scrollTop += covered + 12;
            }
        }
        for (const fe of box.querySelectorAll('[data-err]'))
            fe.textContent = markBidi(errs.get(sid)?.[fe.dataset.err ?? ''] ?? '');
        const changed = new Set([...changedKeys(orig.get(sid) ?? {}, cur.get(sid) ?? {}), ...(resets.get(sid) ?? []), ...(clears.get(sid) ?? [])]);
        for (const row of box.querySelectorAll('[data-field]')) {
            const key = row.dataset.field;
            row.classList.toggle('modified', changed.has(key));
            const input = row.querySelector(`#st-${CSS.escape(sid)}-${CSS.escape(key)}`);
            input?.setAttribute('aria-invalid', String(!!errs.get(sid)?.[key]));
        }
    }
    async function save(s, confirm = [], force = [], profile) {
        const c = cur.get(s.id);
        const keys = [...new Set([...changedKeys(orig.get(s.id), c), ...force])];
        const back = [...keys, ...resets.get(s.id), ...clears.get(s.id)][0];
        const values = {};
        for (const k of keys)
            values[k] = c[k];
        const r = await req('POST', '/api/settings', {
            section: s.id, values, confirm, rev: s.rev, security_profile: profile, reset: [...resets.get(s.id)], clear: [...clears.get(s.id)],
        });
        if (r.status === 409 && Array.isArray(r.json.needs_confirm))
            return askConfirm(s, r.json.needs_confirm, confirm);
        if (r.status === 400 && r.json.errors) {
            errs.set(s.id, r.json.errors);
            srvErrs.set(s.id, new Set(Object.keys(r.json.errors)));
            markSection(s.id);
            deps.toast(String(r.json.error ?? 'invalid values'));
            return;
        }
        if (r.status !== 200) {
            deps.toast(String(r.json.error ?? `saving failed (HTTP ${r.status})`));
            if (r.json.conflict) { // this section reloads from the file; the other sections keep their drafts
                reset(s);
                void load(true);
            }
            return;
        }
        const saved = r.json.saved ?? [];
        notes.set(s.id, saved.length ? s.id === 'security' ? 'Saved: tools, cards and L5 now; sandbox at next start.'
            : `Saved: ${applyNote(r.json.apply_text ?? [])}` : 'No change.');
        reset(s); // this section is saved: its draft goes, the others keep theirs
        await load(true);
        refocus(s.id, back);
    }
    function askConfirm(s, items, already) {
        const ok = h('input', { type: 'checkbox' });
        const go = h('button', { class: 'danger', disabled: true, onclick: () => {
                deps.closeModal();
                void save(s, [...already, ...items.map((i) => i.key)]);
            } }, 'Save anyway');
        ok.addEventListener('change', () => { go.disabled = !ok.checked; });
        deps.modal('Weakening the isolation', h('p', {}, 'This change weakens the guarantee that agents in the sandbox see nothing they should not see:'), h('ul', { class: 'st-warn' }, ...items.map((i) => h('li', {}, h('b', {}, i.label + ': '), i.warning))), h('label', { class: 'st-ack' }, ok, ' I understand and want to save it.'), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), go));
    }
    async function action(s, id) {
        if (id === 'open-rules')
            return deps.openRules();
        if (id === 'open-setup')
            return deps.openSetup?.();
        if (id === 'open-http-env')
            return deps.openHttpEnv?.();
        if (id.startsWith('preset-')) {
            const r = await req('POST', '/api/features', { preset: id.slice(7) });
            if (r.status !== 200)
                return deps.toast(String(r.json.error ?? `HTTP ${r.status}`));
            notes.set(s.id, `Preset applied: ${String(r.json.preset)}.`);
            presetNow = String(r.json.preset); // N10-D-4: /api/state comes later - the fold follows the preset now
            deps.changed?.();
            reset(s);
            return load(true);
        }
        if (id === 'verify-gitlab') {
            const r = await req('POST', '/api/settings/verify-gitlab', {});
            notes.set(s.id, r.json.ok ? `The token belongs to user ${String(r.json.username)}${r.json.name ? ` (${String(r.json.name)})` : ''}.`
                : `Verification failed: ${String(r.json.error ?? r.status)}`);
            draw();
        }
        if (id === 'verify-github') {
            const r = await req('POST', '/api/settings/verify-github', {});
            notes.set(s.id, r.json.ok ? `Logged in as ${String(r.json.username)}.`
                : `Verification failed: ${String(r.json.error || r.status)}${r.json.username ? ` (ok: ${String(r.json.username)})` : ''}`);
            draw();
        }
        if (id === 'prod-suggest') {
            const r = await req('POST', '/api/prod/run', { mode: 'suggest' });
            notes.set(s.id, r.status < 300 ? 'Looking for mappings - suggestions appear next to the projects when the session ends (a few minutes).'
                : `Could not start: ${String(r.json.error ?? r.status)}`);
            draw();
        }
        if (id === 'db-install') {
            const r = await req('POST', '/api/db/install', {});
            notes.set(s.id, r.status < 300 ? 'Installing the drivers into dbvenv in Piper\'s home - the Database view shows when they are ready.'
                : `Could not start: ${String(r.json.error ?? r.status)}`);
            draw();
        }
        if (id === 'mm-test') {
            const r = await req('POST', '/api/mattermost/test', {});
            // F22: the result lands in the status line at the top (and a toast), not in a note under nine fields
            const st = r.status < 300 ? r : await req('GET', '/api/mattermost'); // a failed test answers 4xx: the status keeps last_error
            if (st.status < 300)
                mmSt = st.json;
            deps.toast(r.status < 300 && !r.json.last_error ? 'Test message sent.'
                : `Sending failed: ${String(r.json.last_error ?? r.json.error ?? r.status)}`);
            draw();
        }
    }
    // ---------------------------------------------------------------- controls
    function control(s, f) {
        const sid = s.id;
        const v = f.readonly ? f.value : cur.get(sid)?.[f.key];
        const id = `st-${sid}-${f.key}`;
        if (f.type === 'env') {
            return h('code', { class: 'st-env', id }, String(f.value ?? '') || '(not set)');
        }
        if (f.type === 'bool') {
            const i = h('input', { type: 'checkbox', id, role: 'switch' });
            i.checked = !!v;
            i.addEventListener('change', () => setVal(sid, f.key, i.checked));
            // N13: the same switch as Features and the Inbox rules, not a native checkbox
            return h('label', { class: 'switch' }, i, h('span', { class: 'track', 'aria-hidden': 'true' }));
        }
        if (f.type === 'enum') {
            const sel = h('select', { id }, ...(f.choices ?? []).map(([val, label]) => h('option', { value: String(val) }, label)));
            sel.value = String(v ?? '');
            sel.addEventListener('change', () => setVal(sid, f.key, sel.value));
            return sel;
        }
        if (f.type === 'int' || f.type === 'time' || f.type === 'string') {
            const i = h('input', { id, type: f.type === 'int' ? 'number' : f.type === 'time' ? 'time' : 'text', class: f.type === 'string' ? 'st-wide' : '',
                placeholder: f.type === 'time' ? 'HH:MM' : String(f.default ?? ''), min: f.min, max: f.max, autocomplete: 'off', spellcheck: 'false' });
            i.value = v === undefined || v === null ? '' : String(v);
            i.addEventListener('input', () => setVal(sid, f.key, f.type === 'int' ? (i.value === '' ? '' : Number(i.value)) : i.value));
            return i;
        }
        if (f.type === 'list' && f.choices?.length) { // a fixed set (weekdays): toggle buttons, stored in the choices' order
            const picked = new Set(Array.isArray(v) ? v : []);
            const box = h('span', { class: 'st-choices', id, role: 'group' });
            for (const [val, label] of f.choices) {
                // N12: the plain words on the chip, the tool's own name (git_push) in its tooltip
                const b = h('button', { type: 'button', class: 'st-chip' + (picked.has(val) ? ' on' : ''), 'aria-pressed': String(picked.has(val)),
                    title: typeof val === 'string' && /_/.test(val) ? val : undefined }, label);
                b.addEventListener('click', () => {
                    if (picked.has(val))
                        picked.delete(val);
                    else
                        picked.add(val);
                    b.classList.toggle('on', picked.has(val));
                    b.setAttribute('aria-pressed', String(picked.has(val)));
                    setVal(sid, f.key, f.choices.map((c) => c[0]).filter((x) => picked.has(x)));
                });
                box.append(b);
            }
            return box;
        }
        if (f.type === 'list') {
            const t = h('textarea', { id, rows: Math.min(10, Math.max(2, listText(v).split('\n').length + 1)), spellcheck: 'false', class: 'st-list mono' });
            t.value = listText(v);
            t.addEventListener('input', () => setVal(sid, f.key, parseList(t.value, !!f.item_int)));
            return t;
        }
        if (f.type === 'secret') {
            const i = h('input', { id, type: 'password', autocomplete: 'new-password', class: 'st-wide',
                placeholder: f.set ? `set ${f.hint ?? ''} - a new one replaces it` : 'not set' });
            i.value = String(v ?? '');
            i.addEventListener('input', () => setVal(sid, f.key, i.value));
            const del = f.set ? h('button', { class: 'mini', title: 'deletes the secret file', onclick: () => {
                    i.value = '';
                    setVal(sid, f.key, ''); // first: setVal cancels a pending delete (a newly typed secret wins)
                    clears.get(sid).add(f.key);
                    markSection(sid);
                    deps.toast('Deleted on Save');
                } }, 'Delete') : null;
            return h('span', { class: 'st-secret' }, i, del, f.env_fallback ? h('span', { class: 'mut small' }, ' the token from the environment (GITLAB_TOKEN) is used now') : null);
        }
        if (f.type === 'projects')
            return projectsEditor(s, f);
        if (f.type === 'review_actions') {
            let selected = 0;
            const box = h('div', { class: 'st-review-actions' });
            const rows = () => (cur.get(sid)?.[f.key] ?? []);
            const drawActions = () => {
                clear(box);
                const menu = h('div', { class: 'st-review-list' });
                rows().forEach((a, i) => menu.append(h('button', { type: 'button', class: i === selected ? 'active' : '', onclick: () => { selected = i; drawActions(); } }, h('kbd', {}, String(a.key)), markBidi(a.label))));
                const row = rows()[selected];
                if (!row)
                    return void box.append(menu);
                const label = h('input', { value: row.label, 'aria-label': 'Action name' });
                const key = h('input', { type: 'number', min: 1, max: 9, value: row.key, 'aria-label': 'Action shortcut' });
                const kind = h('select', { 'aria-label': 'Action behavior' }, h('option', { value: 'read' }, 'Explain / check'), h('option', { value: 'edit' }, 'Change code'));
                kind.value = row.kind;
                const prompt = h('textarea', { rows: 6, 'aria-label': 'Action prompt', spellcheck: 'false' }, row.prompt);
                const update = () => {
                    setVal(sid, f.key, rows().map((a, i) => i === selected ? { ...a, label: label.value, key: Number(key.value), kind: kind.value, prompt: prompt.value } : a));
                    menu.children[selected]?.replaceChildren(h('kbd', {}, key.value), markBidi(label.value)); // N10-D-3: the list showed the old number / name
                };
                for (const input of [label, key, kind, prompt])
                    input.addEventListener('input', update);
                const variables = h('div', { class: 'row left small' }, ...['target', 'file', 'range', 'side', 'code', 'note'].map(name => h('button', { type: 'button', class: 'mini', onclick: () => { prompt.setRangeText(`{{${name}}}`, prompt.selectionStart, prompt.selectionEnd, 'end'); update(); prompt.focus(); } }, `{{${name}}}`)));
                box.append(menu, h('div', { class: 'st-review-detail' }, h('label', {}, 'Name', label), h('div', { class: 'row' }, h('label', {}, 'Number', key), h('label', {}, 'Behavior', kind)), h('label', {}, 'Prompt template', prompt), variables, h('div', { class: 'row' }, h('button', { type: 'button', disabled: rows().length >= 9, onclick: () => {
                        const used = new Set(rows().map(a => a.key));
                        const number = [1, 2, 3, 4, 5, 6, 7, 8, 9].find(n => !used.has(n));
                        setVal(sid, f.key, [...rows(), { id: 'action-' + Date.now(), key: number, label: 'New action', kind: 'edit', prompt: '{{note}}' }]);
                        selected = rows().length - 1;
                        drawActions();
                    } }, 'Add action'), h('button', { type: 'button', disabled: rows().length <= 1, onclick: () => { setVal(sid, f.key, rows().filter((_, i) => i !== selected)); selected = 0; drawActions(); } }, 'Remove'), h('button', { type: 'button', onclick: () => { setVal(sid, f.key, structuredClone(f.default)); selected = 0; drawActions(); } }, 'Restore defaults'))));
            };
            drawActions();
            return box;
        }
        return h('span', {}, String(v ?? ''));
    }
    function projectsEditor(s, f) {
        const sid = s.id;
        const rows = () => cur.get(sid)[f.key];
        const sugg = () => f.suggestions ?? {};
        const hiddenSugg = new Map();
        const ignored = () => f.ignored ?? [];
        const q = h('input', { type: 'search', class: 'st-psearch', placeholder: 'Search project, path, GitLab…', spellcheck: 'false' });
        q.value = projSearch; // survives the reload after Save
        const filters = [['all', 'all'], ['gitlab', 'with GitLab'], ['obs', 'with observability'],
            ['explicit', 'added by hand'], ['suggested', 'with a suggestion'], ['unmapped', 'without a mapping']];
        const sel = h('select', { class: 'st-pfilter', title: 'filter' }, ...filters.map(([v, l]) => h('option', { value: v }, l)));
        sel.value = readStore('piper.settings.pfilter') ?? 'all';
        const count = h('span', { class: 'mut small' });
        const body = h('tbody', {});
        const edit = (i, patch) => {
            const next = structuredClone(rows());
            Object.assign(next[i], patch);
            setVal(sid, f.key, next);
        };
        const ignore = async (key, on) => {
            const r = await req('POST', '/api/prod/ignore', { project: key, on });
            if (r.status !== 200)
                return deps.toast(String(r.json.error ?? `HTTP ${r.status}`));
            f.ignored = r.json.ignored ?? [];
            if (on && f.suggestions?.[key]) { // kept aside: "undo" brings the suggestion back without a reload
                hiddenSugg.set(key, f.suggestions[key]);
                delete f.suggestions[key];
            }
            else if (!on && hiddenSugg.has(key)) {
                (f.suggestions ??= {})[key] = hiddenSugg.get(key);
                hiddenSugg.delete(key);
            }
            deps.toast(on ? `${key}: a mapping will no longer be offered` : `${key}: may be suggested again`);
            fill();
        };
        const fill = () => {
            clear(body);
            const idx = filterProjects(rows(), q.value, sel.value, sugg(), ignored());
            count.textContent = markBidi(`${idx.length} of ${rows().length}`);
            for (const i of idx) {
                const r = rows()[i];
                const inp = (k, ph, cls = '') => {
                    const v = k === 'gitlab' ? (Array.isArray(r.gitlab) ? r.gitlab.join(', ') : String(r.gitlab ?? '')) : String(r[k] ?? '');
                    const el2 = h('input', { value: v, placeholder: ph, spellcheck: 'false', class: cls });
                    el2.addEventListener('input', () => edit(i, { [k]: k === 'gitlab' ? el2.value.split(/[,\s]+/).filter(Boolean) : el2.value }));
                    return el2;
                };
                const src = r.explicit ? 'by hand' : r.source === 'glob' ? 'pattern' : 'found';
                const obsCell = h('td', { class: 'st-obs' }, inp('obs', 'not checked'));
                const sg = sugg()[r.key];
                if (!r.obs && sg) {
                    obsCell.append(h('div', { class: 'st-sugg small', title: sg.note }, `suggestion: ${sg.obs} `, h('button', { class: 'link small', onclick: () => { edit(i, { obs: sg.obs }); fill(); } }, 'Use'), ' · ', h('button', { class: 'link small', onclick: () => void ignore(r.key, true) }, "Don't map")));
                }
                else if (!r.obs && ignored().includes(r.key)) {
                    obsCell.append(h('div', { class: 'mut small' }, 'not suggested · ', h('button', { class: 'link small', onclick: () => void ignore(r.key, false) }, 'undo')));
                }
                body.append(h('tr', { class: r.explicit ? 'explicit' : '' }, h('td', {}, r.explicit ? inp('key', 'client/repo', 'mono') : h('span', { class: 'mono' }, r.key), h('div', { class: 'mut small' }, src)), h('td', { class: 'st-path' }, r.explicit ? inp('path', '/path/to/checkout', 'mono small') : h('span', { class: 'mono small', title: r.path }, r.path)), h('td', {}, inp('gitlab', 'from origin', 'mono')), obsCell, h('td', {}, r.explicit ? h('button', { class: 'icon st-prm', title: 'remove from projects.json (the checkout on disk stays and may show up again as found)', 'aria-label': 'remove the project', onclick: () => {
                        setVal(sid, f.key, rows().filter((_, j) => j !== i));
                        fill();
                    } }, fi('x')) : null)));
            }
            if (!idx.length)
                body.append(h('tr', {}, h('td', { colspan: 5, class: 'mut' }, 'Nothing matches.')));
        };
        q.addEventListener('input', () => { projSearch = q.value; fill(); });
        sel.addEventListener('change', () => { writeStore('piper.settings.pfilter', sel.value); fill(); });
        const add = h('button', { class: 'mini', onclick: () => {
                setVal(sid, f.key, [{ key: '', path: '', gitlab: [], profile: '', title: '', obs: '', explicit: true, source: 'projects.json' }, ...rows()]);
                q.value = projSearch = '';
                sel.value = 'all';
                fill();
                body.querySelector('input')?.focus();
            } }, fi('plus'), 'Add a project by hand');
        const box = h('div', { class: 'st-projects' }, h('div', { class: 'row left st-ptools' }, q, sel, count, h('span', { class: 'grow' }), add), h('div', { class: 'st-pwrap' }, h('table', { class: 'st-ptable' }, h('thead', {}, h('tr', {}, ...['project', 'path', 'GitLab', 'observability (morning check)', ''].map((x) => h('th', {}, x)))), body)));
        fill();
        return box;
    }
    /** F19: Settings › Features = the preset as a segment (Custom picks itself after a manual change), what
     *  the modules amount to ("Custom = Developer + Grid − Diagnostics"), then a row per module (sketch small, label,
     *  one line, switch; a click shows the sketch large). A preset applies at once (as its bar action did); a switch is
     *  a draft like any field - the sticky Save, and Host shells on asks "Weakening the isolation" on Save (piperd's 409).
     *  The gallery with large sketches stays in the welcome (setup.ts). */
    /** F22: Settings › Mattermost = three decisions instead of nine fields: the status line with Send test,
     *  the webhook, the message as a segment, "notify after" in minutes, quiet hours on one row; the rate limit, the exact
     *  seconds, the channel and the sender name folded in Advanced. Same fields, same drafts, the F19 sticky Save. */
    function mattermostEl(s, q) {
        const sid = s.id;
        const c = cur.get(sid) ?? {};
        const fld = (k) => s.fields.find((f) => f.key === k);
        if (!mmAsked) {
            mmAsked = true;
            void req('GET', '/api/mattermost').then((r) => {
                mmSt = r.status < 300 ? r.json : null;
                el.querySelector('.mm-status')?.replaceWith(statusEl());
            }, () => undefined);
        }
        const statusEl = () => {
            const ln = mmStatusLine(mmSt, String(c.channel ?? ''));
            return h('div', { class: 'mm-status mm-' + ln.tone }, h('span', { class: 'mm-dot', 'aria-hidden': 'true' }), h('span', { class: 'grow' }, ln.text), h('button', { disabled: !mmSt?.configured, title: mmSt?.configured ? 'one message through the saved webhook' : 'save a webhook URL first',
                onclick: () => void action(s, 'mm-test') }, 'Send test'));
        };
        const seg = (key, opts, cls = '') => {
            const v = c[key];
            return h('div', { class: 'fm-seg mm-seg ' + cls, role: 'radiogroup', 'aria-label': fld(key)?.label ?? key }, ...opts.map(([val, label, title]) => h('button', { type: 'button', role: 'radio', class: v === val ? 'on' : '', 'aria-checked': String(v === val),
                title, 'data-mm': `${key}:${val}`, onclick: () => { setVal(sid, key, val); draw(); } }, label)));
        };
        const row = (label, ...ctl) => h('div', { class: 'mm-row' }, h('span', { class: 'mm-label' }, label), h('div', { class: 'mm-ctl' }, ...ctl));
        const err = (k) => h('div', { class: 'st-err', 'data-err': k }, errs.get(sid)?.[k] ?? '');
        const after = Number(c.after_s ?? 180);
        const detail = fld('detail');
        const advanced = ['min_gap_s', 'after_s', 'channel', 'username'].map(fld).filter((f) => !!f);
        return h('div', { class: 'mm-form' }, statusEl(), fld('webhook') ? row('Webhook', control(s, fld('webhook')), err('webhook')) : null, detail ? row('Message', seg('detail', (detail.choices ?? []).map(([val, text]) => [val, MM_DETAIL[String(val)] ?? String(val), String(text)]))) : null, row('Notify after', seg('after_s', MM_AFTER.map(([n, l]) => [n, l, `${n} s`])), MM_AFTER.some(([n]) => n === after) ? null : h('span', { class: 'fm-chip', title: 'set exactly in Advanced' }, `${after} s`), h('span', { class: 'mut' }, 'of waiting')), fld('quiet_from') && fld('quiet_to') ? row('Quiet hours', control(s, fld('quiet_from')), h('span', { class: 'mut' }, '–'), control(s, fld('quiet_to')), fld('quiet_weekend') ? h('label', { class: 'mm-check' }, control(s, fld('quiet_weekend')), 'and weekends') : null, h('span', { class: 'mut small' }, 'empty = off'), err('quiet_from'), err('quiet_to')) : null, h('details', { class: 'mm-adv', open: mmAdv || !!q || advanced.some((f) => !!errs.get(sid)?.[f.key]),
            ontoggle: (e) => { mmAdv = e.currentTarget.open; } }, h('summary', {}, mmAdvancedSummary(Number(c.min_gap_s ?? 900))), ...advanced.map((f) => fieldRow(s, f, false, q))));
    }
    function featuresEl(s, fields, all) {
        const fs = deps.features?.();
        const draft = cur.get(s.id) ?? {};
        // the draft decides what the segment and the line say (the page does not redraw on the /api/state poll)
        const diff = fs?.preset_modules ? presetDiff(fs, draft) : null;
        const now = diff ? diff.preset : (presetNow || fs?.preset);
        const list = new Map(fs?.list.map(([id, , text]) => [id, text]) ?? []);
        const rows = fields.map((f) => {
            const on = draft[f.key] === true;
            return moduleRow(f.key, f.label, list.get(f.key) ?? f.help, on, () => { setVal(s.id, f.key, !on); draw(); }, {
                cls: on !== (orig.get(s.id)?.[f.key] === true) ? 'changed' : '',
                open: featOpen.has(f.key),
                toggle: () => { if (featOpen.has(f.key))
                    featOpen.delete(f.key);
                else
                    featOpen.add(f.key); draw(); },
                badge: f.security ? h('span', { class: 'st-badge', title: 'turning it on weakens the isolation: Save asks first' }, 'isolation') : null
            });
        });
        const seg = all && fs?.presets.length ? h('div', { class: 'fm-seg', role: 'radiogroup', 'aria-label': 'preset' }, ...fs.presets.map(([id, label, text]) => h('button', { type: 'button', role: 'radio', class: now === id ? 'on' : '', 'aria-checked': String(now === id),
            'data-fk': `preset:${id}`, title: text, onclick: () => void action(s, 'preset-' + id) }, label)), h('span', { class: 'fm-custom' + (now === 'custom' ? ' on' : ''), role: 'radio', 'aria-checked': String(now === 'custom'), 'aria-disabled': 'true',
            title: 'your own mix - picked by itself when you switch a module; a preset starts over' }, 'Custom')) : null;
        const chip = (x) => h('span', { class: 'fm-chip' }, x);
        const line = all && diff && diff.label ? h('div', { class: 'fm-line small' }, ...(now === 'custom'
            ? [h('span', { class: 'mut' }, `Custom = ${diff.label}`), ...(diff.added.length ? [h('span', { class: 'mut' }, ' +'), ...diff.added.map(chip)] : []),
                ...(diff.removed.length ? [h('span', { class: 'mut' }, ' − ' + diff.removed.join(', '))] : [])]
            : [h('span', { class: 'mut' }, `${diff.label} =`), ...presetChips(fs, diff.base).map(chip)])) : null;
        return h('div', { class: 'st-feat' }, seg, line, h('div', { class: 'fm-list' }, ...rows));
    }
    /** The Security redesign: one Settings panel, with details unfolded in place. */
    function securityEl(s, f, q) {
        const tree = f.tree ?? [];
        const v = (cur.get(s.id)?.[f.key] ?? {});
        const was = (orig.get(s.id)?.[f.key] ?? {});
        const all = tree.flatMap(secItems);
        const ids = all.map(i => i.id);
        const github = deps.features?.()?.modules?.github !== false;
        const visibleTree = secVisibleTree(tree, github);
        const hits = q ? secSearchAll || s.title.toLowerCase().includes(q.trim().toLowerCase()) ? null : secMatches(visibleTree, q) : secOnly;
        const near = secNearest(v, f.presets ?? {}, ids);
        const plain = !!f.presets?.plain && secMatchesPreset(v, f.presets.plain, ids);
        const selectedProfile = secSelectedProfile(v, was, f.profile, f.presets ?? {}, ids);
        const exact = SEC_LEVELS.find(([id]) => id === selectedProfile);
        const put = (items, on) => {
            setVal(s.id, f.key, secSetItems(v, items, on));
            draw();
        };
        const sw = (items, label) => {
            const editable = items.filter(i => !i.required);
            const [on, count] = secCount(editable, v);
            const input = h('input', { type: 'checkbox', role: items.length > 1 ? 'checkbox' : 'switch', id: 'sc-switch-' + (items.length > 1 ? 'all-' : '') + items[0]?.id, 'aria-label': label,
                disabled: !editable.length, title: !editable.length ? "Production DB and production or unknown HTTP always require your authorization in every profile; Strict may deny them. Session grants are not guaranteed." : undefined });
            input.checked = count ? on === count : true;
            input.indeterminate = on > 0 && on < count;
            input.setAttribute('aria-checked', input.indeterminate ? 'mixed' : String(input.checked));
            input.addEventListener('change', () => {
                const focused = document.activeElement === input;
                put(items, input.checked);
                if (focused)
                    document.getElementById(input.id)?.focus({ preventScroll: true });
            });
            return h('label', { class: 'switch' + (input.indeterminate ? ' sc-mixed' : '') }, input, h('span', { class: 'track', 'aria-hidden': 'true' }));
        };
        const row = (label, help, controls, attrs = {}, extra = []) => h('div', { class: 'st-field', ...attrs }, h('div', { class: 'st-description' }, h('div', { class: 'st-label' }, ...label), h('div', { class: 'st-help mut small' }, ...help), ...extra), h('div', { class: 'st-ctl' }, ...controls));
        const cell = (i, approval) => {
            const changed = (v[i.id] === true) !== (was[i.id] === true);
            const { help, tag } = secTag(i.help ?? '');
            const code = (mobile = false) => h('span', { class: 'mono sc-kind-code' + (mobile ? ' sc-mobile-code' : '') }, ...marked(i.kind ?? '', q));
            return row([...marked(approval ? secKindLabel(i) : i.label, q),
                ...(!approval && tag ? [h('span', { class: 'mono sc-tag' }, tag)] : []),
                ...(i.required ? [h('span', { class: 'sc-required' }, fi('lock'), 'Required')] : [])], [...marked(i.required ? 'Always asks. You can allow it for one session on the approval card.' : help, q)], [h('div', { class: 'sc-switchline' }, sw([i], i.required ? 'Required approval for ' + i.label : i.label), approval && i.kind ? code() : null)], { class: 'st-field sc-option' + (approval ? ' sc-kind' : '') + (v[i.id] ? ' on' : '') + (changed ? ' modified' : '') + (secFocus === i.id ? ' focus' : ''), 'data-sec': i.id }, [...(approval && i.kind ? [code(true)] : []),
                ...(!approval && !v[i.id] && i.risk ? [h('div', { class: 'sc-off small' }, ...marked(kbd(i.risk), q))] : [])]);
        };
        const area = (id, title, help, items, approval, applies = '') => {
            const shown = items.filter(i => !hits || hits.has(i.id));
            if (!shown.length)
                return null;
            const open = !!hits || secOpen.has(id) || items.some(i => i.id === secFocus);
            const changed = items.some(i => (v[i.id] === true) !== (was[i.id] === true));
            const summary = approval ? secAskSentence(items, v) : null;
            const [n, count] = secCount(items, v);
            const required = items.filter(i => i.required).length;
            const toggle = h('button', { type: 'button', 'aria-expanded': String(open), 'aria-controls': 'sc-options-' + id,
                'aria-label': `${open ? 'Hide' : 'Change'} ${title}`, onclick: () => {
                    if (secOpen.has(id))
                        secOpen.delete(id);
                    else
                        secOpen.add(id);
                    writeStore('piper.settings.secopen', JSON.stringify([...secOpen]));
                    draw();
                } }, open ? 'Hide' : 'Change', fi('chev-d'));
            const description = approval ? `Asks before ${summary.asks}.`
                : secAlways([{ id, title, help, items }], v).length ? 'Protects ' + secAlways([{ id, title, help, items }], v).join(', ') + '.' : 'Nothing switched on — like a plain hole.';
            const head = row([...marked(title, q)], [markBidi(description)], [h('div', { class: 'sc-area-actions' }, h('span', { class: 'mut small' }, `${n} of ${count} ${approval ? 'ask' : 'on'}${required ? ` · ${required} required` : ''}`), toggle),
                ...(applies ? [h('span', { class: 'st-apply' }, applies)] : [])], { class: 'st-field sc-area' + (changed ? ' modified' : '') }, (approval && !summary.own) ? [] : [h('div', { class: 'st-help sc-own small' }, markBidi(approval ? 'On its own: ' + summary.own + '.' : help))]);
            return h('div', { class: 'sc-area-group', 'data-area': id }, head, h('div', { class: 'sc-options', id: 'sc-options-' + id, hidden: !open }, ...(open ? [
                row([`All ${title}${approval ? ' approvals' : ' protections'}`], approval && required ? ['Required approvals stay on.'] : [], [h('div', { class: 'sc-switchline' }, sw(items, `All optional ${title}`), h('span', { class: 'mut small' }, `${n} of ${count}`))], { class: 'st-field sc-all' }), ...shown.map(i => cell(i, approval))
            ] : [])));
        };
        const visibleItems = visibleTree.flatMap(secItems);
        const required = all.filter(i => i.required).length;
        const levels = h('div', { class: 'sc-profiles', role: 'radiogroup', 'aria-label': 'Profile', 'aria-describedby': 'sc-profile-lead' }, ...SEC_LEVELS.map(([id, label, description]) => {
            const current = exact?.[0] === id;
            const [on, total] = secCount(visibleItems, f.presets?.[id] ?? {});
            return h('button', { type: 'button', 'data-profile': id,
                class: 'sn-choice sc-level sc-profile-card' + (current ? ' on' : ''), role: 'radio', 'aria-checked': String(current), 'aria-pressed': String(current),
                onclick: () => { if (!current)
                    presetAsk(s, f, id, label); } }, h('span', { class: 'sn-choice-title' }, h('span', { class: 'sc-radio', 'aria-hidden': 'true' }), h('b', {}, label), current ? h('span', { class: 'sn-current' }, 'CURRENT') : null), h('span', { class: 'sn-choice-body sc-profile-body' }, h('span', { class: 'mut small' }, description), h('span', { class: 'sc-profile-count small' }, `${on} of ${total} switches on`)));
        }));
        const changes = near.changed.length;
        const order = (id) => { const i = SEC_ASK_ORDER.indexOf(id); return i < 0 ? SEC_ASK_ORDER.length : i; };
        const asks = (tree.find(g => g.groups)?.groups ?? []).filter(g => g.id !== 'github' || github).sort((a, b) => order(a.id) - order(b.id));
        const heading = (text, timing) => h('h3', { class: 'sc-heading' }, text, h('span', {}, timing));
        return h('div', { class: 'sc-panel' }, h('div', { class: 'st-fieldgroup sc-profile-block' }, heading('Profile', 'Asks first · saves at once'), h('p', { class: 'sc-profile-lead', id: 'sc-profile-lead' }, 'Start from a ready-made profile. Applying one ', h('strong', {}, `sets all ${visibleItems.length} approvals and protections`), ' on this page.'), levels, !exact ? h('div', { class: 'sn-own sc-profile-status on', role: 'status' }, h('div', { class: 'sc-profile-status-copy' }, h('div', { class: 'sn-choice-title' }, h('span', { class: 'sc-radio', 'aria-hidden': 'true' }), h('b', {}, plain ? 'Plain hole' : 'Custom'), h('span', { class: 'sn-current' }, 'CURRENT'), unsavedCount(s.id) ? h('span', { class: 'sc-profile-unsaved small' }, '· Unsaved') : null), h('div', { class: 'sc-profile-status-help mut small' }, plain ? `Only the ${required} required approvals are on. Every optional guard and approval is off.`
            : "Custom: your saved switches are kept as they are. They never auto-upgrade to profile rules, even if they match a profile. Apply a profile to change them.")), h('div', { class: 'sc-custom-actions' }, !plain && changes ? h('button', { class: 'link small', onclick: () => { secOnly = new Set(near.changed); draw(); } }, 'Show changes') : null, h('button', { class: 'small', onclick: () => presetAsk(s, f, plain ? 'hole' : near.id, plain ? 'Minimal' : near.label) }, `Reset to ${plain ? 'Minimal' : near.label}…`))) : null, h('div', { class: 'sc-profile-foot' }, h('div', { class: 'sc-profile-required mut small' }, fi('lock'), h('span', {}, "Production DB and production or unknown HTTP always require your authorization in every profile; Strict may deny them. Session grants are not guaranteed.")), !plain ? h('button', { class: 'link small sc-plain', onclick: () => presetAsk(s, f, 'plain', 'Plain hole') }, 'Turn off optional guards…') : null)), q && !s.title.toLowerCase().includes(q.trim().toLowerCase()) ? h('div', { class: 'sc-filter st-shown small' }, `${secMatches(visibleTree, q).size} of ${visibleTree.flatMap(secItems).length} switches match “${q.trim()}”`, h('button', { class: 'link small', onclick: () => { secSearchAll = !secSearchAll; draw(); } }, secSearchAll ? 'Only matching' : 'Show all')) : null, secOnly && !q ? h('div', { class: 'sc-filter small' }, `Only the ${secOnly.size} changed switches`, h('button', { class: 'link small', onclick: () => { secOnly = null; draw(); } }, 'Show all')) : null, h('div', { class: 'st-fieldgroup' }, heading('Approvals', 'Set by the profile · applies at once'), ...asks.map(g => area(g.id, g.short ?? g.title, '', g.items, true))), h('div', { class: 'st-fieldgroup' }, heading('Protections', 'Set by the profile · never ask · enforced while on'), ...tree.filter(g => g.items).map(g => area(g.id, g.title, g.help, g.items, false, g.id === 'start' ? 'Next start · watching for new files applies at once' : g.id === 'sandbox' ? 'Next start · tool rules apply at once' : 'At once'))));
    }
    /** a preset replaces every switch: asked first, then saved at once */
    function presetAsk(s, f, id, label) {
        const want = f.presets?.[id];
        if (!want) {
            deps.toast('This profile needs the updated Piper server. Reload after restarting Piper.');
            return;
        }
        const ids = (f.tree ?? []).flatMap(secItems).map((i) => i.id);
        const go = h('button', { class: 'primary', onclick: () => {
                deps.closeModal();
                secOnly = null;
                secSearchAll = false;
                setVal(s.id, f.key, Object.fromEntries(ids.map((i) => [i, want[i] === true])));
                void save(s, [], [f.key], id);
            } }, id === 'plain' ? 'Turn off' : `Apply ${label}`);
        const descriptions = {
            recommended: "Apply Recommended? This sets the Recommended switches and profile rules. Later manual checkbox changes save the result as Custom.",
            hole: "Apply Minimal? This sets the Minimal switches and profile rules. Later manual checkbox changes save the result as Custom.",
            guarded: "Apply Strict? This sets the Strict switches and profile rules, denying prod DB writes, merge and CI. Manual checkbox changes save as Custom.",
            plain: 'Turn off optional Piper guards and approval cards. Production DB and production or unknown-target HTTP approvals stay required; you can allow them for a session. Hole’s own configuration still applies.',
        };
        const current = (cur.get(s.id)?.[f.key] ?? {});
        const visibleTree = secVisibleTree(f.tree ?? [], deps.features?.()?.modules?.github !== false);
        const visibleIds = visibleTree.flatMap(secItems).map(i => i.id);
        const total = visibleIds.length;
        const changed = secChangedCount(visibleTree, current, want);
        const on = visibleIds.filter(i => want[i] === true).length;
        const required = (f.tree ?? []).flatMap(secItems).filter(i => i.required).length;
        deps.modal(id === 'plain' ? 'Turn off optional guards?' : `Apply the ${label} profile?`, h('p', { class: 'mut' }, descriptions[id] ?? ''), h('div', { class: 'sc-profile-impact' }, h('strong', {}, `Sets all ${total} approvals and protections on this page.`), h('p', { class: 'mut small' }, `${f.profile === 'custom' ? 'Custom' : secLevelName(current, f.presets ?? {}, ids)} → ${label}: ${changed} switch${changed === 1 ? '' : 'es'} change. ${on} of ${total} will be on.`), h('div', { class: 'sc-profile-required mut small' }, fi('lock'), `The ${required} required approvals stay on.`)), unsavedCount(s.id) ? h('p', { class: 'mut small' }, `Your ${unsavedCount(s.id)} unsaved changes in this section are replaced.`) : null, h('p', { class: 'mut small' }, 'Tool rules, cards and L5 apply at once. Sandbox contents and start checks apply from the next start; running sessions keep their current sandbox.'), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), go));
    }
    /** 9.46: a field that only matters while a Security switch is on says so (dimmed) and links to the switch */
    function governedNote(f) {
        const g = f.governed_by;
        if (!g || g.on)
            return null;
        return h('div', { class: 'sc-governed small' }, secIcon('shield-off'), h('span', {}, `Used only while Security › ${g.label} is on · off now`), h('button', { class: 'link small', onclick: () => {
                const grp = (sections.find((x) => x.id === 'security')?.fields[0]?.tree ?? []).find((t) => secItems(t).some((i) => i.id === g.id));
                if (grp) {
                    secOpen.add(grp.groups?.find(sg => sg.items.some(i => i.id === g.id))?.id ?? grp.id);
                }
                search.value = query = '';
                secOnly = null;
                secFocus = g.id;
                select('security');
            } }, 'Turn on', fi('chev-r')));
    }
    function fieldRow(s, f, saidAbove = false, q = '') {
        // a module switch has no fixed default (it follows the preset): no "default" button there
        const canReset = !f.readonly && f.type !== 'secret' && f.type !== 'projects' && f.type !== 'env' && f.is_default === false && s.id !== 'features';
        const wide = f.type === 'projects' || f.type === 'review_actions';
        const copy = settingsFieldCopy(f);
        const ctl = control(s, f);
        if (f.readonly) {
            const inputs = [ctl, ...ctl.querySelectorAll('input,select,textarea,button')];
            for (const element of inputs)
                if ('disabled' in element)
                    element.disabled = true;
        }
        const input = ctl.id ? ctl : ctl.querySelector(`#st-${CSS.escape(s.id)}-${CSS.escape(f.key)}`);
        input?.setAttribute('aria-describedby', `st-help-${s.id}-${f.key} st-error-${s.id}-${f.key}`);
        return h('div', { 'data-field': f.key, class: 'st-field' + (f.security ? ' sec' : '') + (wide ? ' wide' : '') + (f.governed_by && !f.governed_by.on ? ' governed' : '') }, wide ? null : h('div', { class: 'st-description' }, h('label', { class: 'st-label', for: `st-${s.id}-${f.key}`, title: f.env || undefined }, ...marked(kbd(copy.label), q)), h('div', { class: 'st-help mut small', id: `st-help-${s.id}-${f.key}` }, ...marked(kbd(copy.summary), q)), copy.detail ? h('details', { class: 'st-more small', open: !!q && copy.detail.toLowerCase().includes(q.trim().toLowerCase()) }, h('summary', {}, 'Technical details'), h('div', { class: 'mut' }, ...marked(kbd(copy.detail), q))) : null, f.security ? h('span', { class: 'st-badge', title: 'weakening the isolation needs confirmation' }, 'Changes to protection may require confirmation') : null, governedNote(f)), h('div', { class: 'st-ctl' }, ctl, wide && f.help ? h('div', { class: 'mut small', id: `st-help-${s.id}-${f.key}` }, ...marked(kbd(f.help), q)) : null, canReset ? h('button', { class: 'st-reset', title: `back to the default: ${listText(f.default) || String(f.default ?? '')}`, onclick: () => {
                resets.get(s.id).add(f.key);
                cur.get(s.id)[f.key] = structuredClone(f.default);
                clearErr(s.id, f.key); // N10-D-1: the typed value's error went with it (it kept Save disabled)
                draw();
                deps.toast('The default value applies on Save');
            } }, fi('history'), 'Reset to default') : null, h('div', { class: 'st-err', id: `st-error-${s.id}-${f.key}`, 'data-err': f.key, 'aria-live': 'polite' }, errs.get(s.id)?.[f.key] ?? ''), f.type === 'list' && !f.choices?.length ? h('span', { class: 'mut small' }, 'One item per line') : null, f.type === 'env' || f.readonly ? h('div', { class: 'st-apply small' }, 'Read-only', f.readonly_reason || f.env ? ` · ${f.readonly_reason || f.env}` : '')
            : f.apply_text && (!saidAbove || f.apply === 'now') ? h('div', { class: 'st-apply small' }, f.apply_text) : null));
    }
    function fieldGroups(s, fields, shared, q) {
        return settingsFieldGroups(s.id, fields).map((group, index) => h('div', { class: 'st-fieldgroup', id: `st-group-${index}` }, group.title ? h('h3', {}, group.title) : null, ...group.fields.map((f) => f.type === 'security' ? securityEl(s, f, q) : fieldRow(s, f, !!shared && f.apply_text === shared, q))));
    }
    let selected = readStore('piper.settings.sec') ?? '';
    /** the advanced group unfolded (remembered; its first default follows the preset) */
    let advOpen = readStore('piper.settings.adv') === null ? null : readStore('piper.settings.adv') === '1';
    /** a preset applied on this page, until the page is opened again (deps.advanced reads /api/state, a poll behind) */
    let presetNow = '';
    /** 9.46: the unfolded Security groups (remembered) and the item a "Turn on ›" link came for */
    const secOpen = new Set((() => { try {
        return JSON.parse(readStore('piper.settings.secopen') ?? '[]');
    }
    catch {
        return [];
    } })());
    let secFocus = '';
    /** F19: the module rows whose sketch is open */
    const featOpen = new Set();
    /** F22: Mattermost's status (GET /api/mattermost; asked again when the section is opened or saved) and Advanced unfolded */
    let mmSt = null;
    let mmAsked = false;
    let mmAdv = false;
    /** F26: the overview's "Show" - only the switches that differ from the nearest level */
    let secOnly = null;
    const groupTitle = (g) => groups.find(([id]) => id === g)?.[1] ?? g;
    function select(id) {
        selected = id;
        mmAsked = false;
        showAll = false;
        writeStore('piper.settings.sec', id);
        draw();
    }
    function draw() {
        const typing = document.activeElement === search;
        const caret = [search.selectionStart, search.selectionEnd];
        const old = el.querySelector('section.st-sec'); // a switch redraws the page: the section keeps its scroll
        const top = old?.dataset.sid === selected ? old.scrollTop : 0;
        const navTop = el.querySelector('nav.st-side')?.scrollTop ?? 0;
        const navLeft = el.querySelector('nav.st-side')?.scrollLeft ?? 0;
        clear(el);
        if (loading && !sections.length) {
            el.append(h('p', { class: 'mut' }, h('span', { class: 'spinner' }), ' loading…'));
            return;
        }
        if (loadErr)
            el.append(h('div', { class: 'st-load-error', role: 'alert' }, h('p', { class: 'warn' }, loadErr), h('button', { onclick: () => void load(true) }, 'Try again')));
        if (!sections.length)
            return;
        if (!sections.some((s) => s.id === selected))
            selected = sections[0].id;
        const hits = searchSettings(sections, query, deps.features?.()?.modules?.github !== false);
        if (hits?.size && !hits.has(selected))
            selected = sections.find((s) => hits.has(s.id)).id;
        const nav = h('nav', { class: 'st-side', 'aria-label': 'Settings sections' }, h('div', { class: 'st-navtitle' }, 'Settings'), h('label', { class: 'st-searchbox' }, searchIcon(), search));
        if (hits) {
            const n = [...hits.values()].reduce((a, k) => a + k.length, 0);
            nav.append(hits.size
                ? h('div', { class: 'st-found small' }, `${n} setting${n === 1 ? '' : 's'} in ${hits.size} section${hits.size === 1 ? '' : 's'}`)
                : h('div', { class: 'st-found' }, h('div', { class: 'mut small' }, `Nothing matches “${query.trim()}”.`), h('div', { class: 'small' }, 'Searched labels, help, keys and choices of all sections.'), h('button', { class: 'link', onclick: () => { search.value = query = ''; draw(); search.focus(); } }, 'Clear search')));
        }
        let group = '';
        const adv = !!hits?.size || (advOpen ?? (presetNow ? presetNow !== 'basic' : (deps.advanced?.() ?? true)));
        for (const s of sections) {
            if (hits && !hits.has(s.id) && s.id !== selected)
                continue; // nothing matches: the open section stays reachable
            const g = s.group ?? 'general';
            if (g !== group) {
                group = g;
                nav.append(g === 'advanced' && !hits?.size // a search unfolds it: no fold button meanwhile
                    ? h('button', { class: 'st-gtitle st-gfold', 'aria-expanded': String(adv), title: 'isolation, hole defaults, the environment', onclick: () => {
                            advOpen = !adv;
                            writeStore('piper.settings.adv', advOpen ? '1' : '0');
                            draw();
                        } }, fi('chev-d', adv ? '' : 'closed'), groupTitle(g))
                    : h('div', { class: 'st-gtitle' }, groupTitle(g)));
            }
            if (g === 'advanced' && !adv && s.id !== selected)
                continue;
            nav.append(h('button', { class: 'st-navi' + (s.id === selected ? ' on' : ''), 'aria-current': s.id === selected ? 'page' : undefined, 'data-nav': s.id, onclick: () => select(s.id) }, s.title, hits?.size ? h('span', { class: 'st-cnt' }, String(hits.get(s.id).length))
                : s.id === 'security' ? secNavCount(s) : navCount(s), h('span', { class: 'st-dot', title: 'unsaved changes' }), s.error ? h('span', { class: 'st-badge' }, '!') : null));
        }
        const s = sections.find((x) => x.id === selected);
        const editable = s.fields.some((f) => !f.readonly && f.type !== 'env');
        // the same "applies after …" under every row is noise: say it once under the title (N11: the majority's too)
        const apply = sharedApply(s.fields);
        const shared = apply.text;
        const q = hits?.size ? query : '';
        const match = hits?.get(s.id);
        const fields = match && !showAll ? s.fields.filter((f) => match.includes(f.key)) : s.fields;
        const mmView = s.id === 'mattermost' && fields === s.fields; // F22: a search narrowing it shows the plain rows
        const crumb = settingsCrumb(groups, sections, s.id);
        if (s.id === 'servers')
            servers.show();
        if (s.id === 'mobile')
            mobile.show();
        const content = s.id === 'mobile' ? [mobile.el] : s.id === 'servers' ? [servers.el] : s.id === 'features' ? [featuresEl(s, fields, fields === s.fields)]
            : mmView ? [mattermostEl(s, q)] : fieldGroups(s, fields, shared, q);
        const box = h('section', { class: 'st-sec', 'data-sid': s.id }, h('div', { class: 'st-crumb' }, h('button', { class: 'link', title: `the first section of ${crumb.group}`, onclick: () => select(crumb.first) }, crumb.group), h('span', { class: 'st-crumb-sep' }, ' › '), crumb.title), h('h2', {}, ...marked(s.title, q)), s.help ? h('p', { class: 'mut' }, kbd(s.help)) : null, match && match.length < s.fields.length ? h('p', { class: 'st-shown small' }, showAll ? `All ${s.fields.length} settings · ${match.length} match “${query.trim()}” ` : `${match.length} of ${s.fields.length} settings match “${query.trim()}” `, h('button', { class: 'link small', onclick: () => { showAll = !showAll; draw(); } }, showAll ? 'Only matching' : 'Show all')) : null, shared ? h('p', { class: 'st-apply small' }, (apply.all ? shared[0].toUpperCase() + shared.slice(1) : `Unless a setting says otherwise, it ${shared}`) + '.') : null, s.group === 'advanced' && editable ? h('p', { class: 'warn small' }, 'Technical settings of the isolation and the environment. A change that weakens the protection needs confirmation.') : null, s.error ? h('p', { class: 'warn' }, s.error) : null, ...content, (s.actions.some((a) => (s.id !== 'features' || !a.id.startsWith('preset-')) && !(mmView && a.id === 'mm-test')) || notes.get(s.id)) ? h('div', { class: 'st-bar' }, ...s.actions.filter((a) => s.id !== 'features' || !a.id.startsWith('preset-')).filter((a) => !(mmView && a.id === 'mm-test'))
            .map((a) => h('button', { title: a.help, onclick: () => void action(s, a.id) }, a.label)), notes.get(s.id) ? h('span', { class: 'st-note small' }, notes.get(s.id)) : null) : null, editable ? h('div', { class: 'st-savebar', hidden: true }, h('span', { class: 'st-sdot', 'aria-hidden': 'true' }), h('span', { class: 'st-scount grow' }), h('button', { class: 'ghost st-undo', disabled: true, onclick: () => { reset(s); draw(); } }, 'Discard'), h('button', { class: 'primary st-save', disabled: true, title: 'Save this section (⌘S)', onclick: () => void save(s) }, 'Save changes', h('kbd', {}, '⌘S'))) : null);
        el.append(h('div', { class: 'st-layout' }, nav, box));
        box.scrollTop = top;
        nav.scrollTop = navTop;
        nav.scrollLeft = navLeft;
        revealNavItem(nav);
        if (secFocus) { // came from a "Turn on ›" link: the switch it named, once
            box.querySelector(`[data-sec="${CSS.escape(secFocus)}"]`)?.scrollIntoView({ block: 'center' });
            secFocus = '';
        }
        for (const x of sections)
            markSection(x.id);
        if (typing) { // the redraw rebuilt the page around the search box: keep typing where it was
            search.focus();
            search.setSelectionRange(caret[0], caret[1]);
        }
    }
    return {
        el,
        refreshServers: () => servers.refresh(),
        /** Open the exact approval switch; no draft or saved policy value is changed. */
        showRule(kind) {
            const id = 'ask.' + kind;
            const grp = (sections.find((x) => x.id === 'security')?.fields[0]?.tree ?? []).find((t) => secItems(t).some((i) => i.id === id));
            if (grp)
                secOpen.add(grp.groups?.find(sg => sg.items.some(i => i.id === id))?.id ?? grp.id);
            selected = 'security';
            showAll = false;
            secOnly = null;
            secFocus = id;
            search.value = query = '';
            if (!sections.length)
                void load();
            else
                draw();
        },
        /** section: open it; filter: the projects table's filter (the board's "review suggestions" -> suggested) */
        show(section, filter) {
            presetNow = '';
            if (section) {
                if (query && !searchSettings(sections, query, deps.features?.()?.modules?.github !== false)?.has(section))
                    search.value = query = ''; // the search would hide it
                selected = section;
                showAll = false;
                writeStore('piper.settings.sec', section);
            }
            if (filter) {
                writeStore('piper.settings.pfilter', filter);
                projSearch = '';
            }
            if (!sections.length || !dirty())
                void load();
            else
                draw();
        },
        /** unsaved edits in any section (the view switch and beforeunload ask first) */
        dirty,
        /** throw the edits away (the user chose to leave) */
        discard() {
            servers.discard();
            for (const s of sections)
                reset(s);
        },
    };
    /** F19: a list section counts its rows in the nav (Projects 43) */
    function navCount(s) {
        if (s.id === 'mobile')
            return mobile.count() ? h('span', { class: 'st-cnt' }, String(mobile.count())) : null;
        if (s.id === 'servers')
            return servers.count() ? h('span', { class: 'st-cnt' }, String(servers.count())) : null;
        const f = s.fields.find((x) => x.type === 'projects');
        const n = f && Array.isArray(f.value) ? f.value.length : 0;
        return n ? h('span', { class: 'st-cnt' }, String(n)) : null;
    }
    /** the Security nav item names the saved level (F19/F26: "Recommended", not an unexplained "12"); the count in the tooltip */
    function secNavCount(s) {
        const f = s.fields.find((x) => x.type === 'security');
        if (!f)
            return null;
        const items = (f.tree ?? []).flatMap(secItems);
        const saved = (orig.get(s.id)?.[f.key] ?? {});
        const n = secCount(items, saved)[0];
        return h('span', { class: 'st-cnt sc-navn', title: `${n} protections on` }, markBidi(f.profile === 'custom' ? 'Custom' : secLevelName(saved, f.presets ?? {}, items.map((i) => i.id))));
    }
    function dirty() {
        return servers.dirty() || sections.some((s) => dirtyOf(s.id));
    }
}
//# sourceMappingURL=settings.js.map