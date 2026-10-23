// W15: one "Start work" dialog for "what do I work on" (IntelliJ's New Project / Get from VCS): the kinds
// on the left (New session · From an issue · Resume conversation, ⌥1-3), the form on the right, one
// fixed size. Pure helpers here (node --test); main.ts startWork() builds the dialog.
/** A dialog's unsaved drafts: switching/filtering projects never writes or loses another draft. */
export function startOptionsDrafts(saved, fallback) {
    const drafts = new Map();
    return (project) => {
        const base = project.split('@')[0] ?? '';
        let draft = drafts.get(base);
        if (!draft) {
            const pref = saved[base];
            draft = { agent: pref?.agent ?? 'claude',
                opts: Object.fromEntries((pref?.opts ?? fallback).map((k) => [k, true])), remember: true };
            drafts.set(base, draft);
        }
        return draft;
    };
}
/** the kinds in the rail; From an issue only with the gitlab or (W23) the github module, named by them */
export function workKinds(gitlab, github = false) {
    const label = gitlab && github ? 'From an issue' : `From a ${gitlab ? 'GitLab' : 'GitHub'} issue`;
    return [
        { id: 'new', label: 'New session', sub: 'a project and a task', icon: '+' },
        ...(gitlab || github ? [{ id: 'issue', label, sub: 'the task links the issue', icon: '#' }] : []),
        { id: 'resume', label: 'Resume conversation', sub: 'go on where it stopped', icon: '↺' },
    ];
}
/** ⌥1-3 (Alt off a Mac) → the index of the kind; by e.code, since ⌥digit types a character on a Mac */
export function kindKey(e) {
    if (!e.altKey || e.metaKey || e.ctrlKey || e.shiftKey)
        return null;
    const m = /^Digit([1-3])$/.exec(e.code ?? '');
    return m ? Number(m[1]) - 1 : null;
}
/** 9.54: the Options summary while an image rebuild is pending ('' = none) */
export function rebuildSummary(pending) {
    return pending ? 'image rebuild at this start ⚠' : '';
}
/** F02: the folded Options line in the foot - every option that is on, or "sandbox default"; the agent has
 *  its own segment beside the project search. Any option on weakens the sandbox: a warning (amber, ⚠). */
export function optsSummary(picked, labels) {
    const on = picked.map((k) => labels[k] ?? k);
    return { text: on.length ? on.join(' · ') : 'sandbox default', risky: on.length > 0 };
}
export function parseForgeUrl(text, github) {
    const t = text.trim();
    const gl = /^https?:\/\/([^/\s]+)\/([^\s]+?)\/-\/(issues|merge_requests)\/(\d+)(?:[/?#]\S*)?$/.exec(t);
    if (gl)
        return { forge: 'gitlab', host: gl[1], path: gl[2], kind: gl[3] === 'issues' ? 'issue' : 'mr', iid: Number(gl[4]) };
    const gh = github ? /^https?:\/\/github\.com\/([^/\s]+\/[^/\s]+)\/(issues|pull)\/(\d+)(?:[/?#]\S*)?$/.exec(t) : null;
    if (gh)
        return { forge: 'github', host: 'github.com', path: gh[1], kind: gh[2] === 'issues' ? 'issue' : 'mr', iid: Number(gh[3]) };
    return null;
}
/** F02: the Piper project a pasted URL belongs to - its GitLab path (or `gh:owner/repo`) in projects.json,
 *  else the one project whose last name segment is the repository's; '' when none or ambiguous */
export function projectForRef(ref, projects) {
    const want = ref.forge === 'github' ? 'gh:' + ref.path : ref.path;
    const linked = projects.filter((p) => (p.gitlab ?? []).some((g) => g.toLowerCase() === want.toLowerCase()));
    if (linked.length === 1)
        return linked[0].key;
    const repo = (ref.path.split('/').pop() ?? '').toLowerCase();
    const named = projects.filter((p) => (p.key.split('/').pop() ?? '').toLowerCase() === repo);
    return named.length === 1 ? named[0].key : '';
}
/** F02: the chip under the task - "#412 title" (or "!7" for a merge request), the title once it is known */
export function refChip(ref, title = '') {
    return `${ref.kind === 'mr' ? '!' : '#'}${ref.iid}${title ? ' ' + title : ''}`;
}
/** F02: the directory every project lives under (their longest common parent) - the list shows paths below it */
export function projectsRoot(paths) {
    if (!paths.length)
        return '';
    const split = paths.map((p) => p.replace(/\/+$/, '').split('/'));
    const out = [];
    for (let i = 0;; i++) {
        const seg = split[0][i];
        // never the path itself: a single project still shows its parent
        if (seg === undefined || split.some((s) => s[i] !== seg || i >= s.length - 1))
            break;
        out.push(seg);
    }
    return out.join('/');
}
/** F02: a project row - its last name segment, and the folder it sits in relative to the root (grey); the full
 *  path goes to the tooltip */
export function projectRow(key, path, root) {
    const name = key.split('/').pop() || key;
    const rel = root && path.startsWith(root + '/') ? path.slice(root.length + 1) : path;
    const dir = rel.split('/').slice(0, -1).join('/');
    return { name, dir };
}
//# sourceMappingURL=startwork.js.map