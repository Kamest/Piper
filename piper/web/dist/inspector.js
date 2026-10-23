// W16: the context panel's Isolation / Network / Session sections as property rows (IntelliJ's Properties:
// label · state · action) with a header that says the state. Pure helpers here (node --test); main.ts
// renderInspector builds the sections.
/** the Isolation header: the check (✓N / ✗ failed / nothing before it ran), then what is unlocked in amber */
export function isoStatus(attest, unlock) {
    const out = [];
    if (attest)
        out.push(attest.ok ? { text: `✓ ${attest.checks} passed`, tone: 'ok', title: `${attest.checks} isolation checks passed` }
            : { text: '✗ failed', tone: 'bad', title: 'the isolation check failed' });
    const u = unlock ?? {};
    // unlocked is the user's deliberate choice: amber with the word, red is for failures
    const on = [u.git ? 'git' : '', u.projects || u.projects_write ? 'projects' : '', u.db_write_other ? 'databases' : ''].filter(Boolean);
    if (on.length)
        out.push({ text: `${out.length ? '· ' : ''}${on.join(' + ')} unlocked`, tone: 'warn', title: 'the session has something unlocked - see the rows below' });
    return out;
}
/** F13 (9.71): the Session header in words - agent · sandbox · how long; the container's name is in the rows */
export function sessionSummary(agent, instance, created, now = Date.now() / 1000) {
    const age = Math.max(0, now - created);
    const long = age < 60 ? 'just started' : age < 3600 ? `${Math.floor(age / 60)} min`
        : age < 86400 ? `${Math.floor(age / 3600)} h ${Math.floor((age % 3600) / 60)} min` : `${Math.floor(age / 86400)} d`;
    return [agent === 'codex' ? 'Codex' : 'Claude', instance ? 'sandbox' : '', long].filter(Boolean).join(' · ');
}
/** the Network header: filtered + how many domains are allowed, or amber "unfiltered"; amber "N refused".
 *  `open` = a request was refused - the one thing in the section you act on */
export function netStatus(net) {
    const n = net.allow?.length ?? 0;
    const bits = [net.unrestricted ? { text: 'unfiltered', tone: 'warn', title: 'hole -u: the egress filter is off' }
            : { text: `filtered · ${n} allowed`, tone: 'mut' }];
    const refused = net.denied?.length ?? 0;
    if (refused)
        bits.push({ text: `· ${refused} refused`, tone: 'warn', title: 'requests the egress filter refused' });
    return { bits, open: refused > 0 };
}
const UNLOCK_LABEL = {
    projects: 'Reading other projects',
    projects_write: 'Editing other projects',
    git: 'Git',
    db_write_other: 'Other projects\' DB writes',
};
/** Only effective locks are shown: all hidden with unlocks off, git hidden in Hole mode. */
export function unlockRows(unlock, gitHole, required = true) {
    if (!required)
        return [];
    const u = unlock ?? {};
    return ['projects', 'projects_write', 'git', 'db_write_other'].filter((k) => k !== 'git' || !gitHole).map((k) => {
        return u[k] ? { key: k, label: UNLOCK_LABEL[k], value: 'unlocked', tone: 'warn', action: 'lock' }
            : { key: k, label: UNLOCK_LABEL[k], value: 'locked', tone: 'mut', action: 'unlock' };
    });
}
/** the fold of a nested <details> by key, not by its summary text (which carries live counts): it outlives a
 *  re-render of the panel */
const detailsOpen = new Map();
export function keepOpen(d, key) {
    const was = detailsOpen.get(key);
    if (was !== undefined)
        d.open = was;
    d.addEventListener('toggle', () => detailsOpen.set(key, d.open));
    return d;
}
//# sourceMappingURL=inspector.js.map