// Pure text helpers of the GUI workbench (unit-tested in tests/piper-unit/workbench.test.mjs).
/** N9: s cut to at most n UTF-16 units without splitting a surrogate pair - an emoji cut in half showed as � in a
 *  shortened title / line ("…x \ud83d…") */
export function cutText(s, n) {
    const c = s.slice(0, n);
    return /[\uD800-\uDBFF]$/.test(c) ? c.slice(0, -1) : c;
}
/** the group of a preflight finding in the blocked panel: uncovered files per root; a settings file never
 *  approved is a first review ('settings-new'), not a change - its diff is the whole file */
export function blockGroupKey(b) {
    if (b.kind === 'uncovered')
        return `${b.kind}|${b.detail ?? ''}`;
    return b.kind === 'settings-changed' && !b.previous ? 'settings-new' : b.kind;
}
/** the domain of a line from hole's network log ("DENIED api.x.com:443 ...") */
export function deniedDomain(line) {
    const m = /((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62})(?::\d+)?/i.exec(line);
    return m ? m[1].toLowerCase() : '';
}
/** the task text for a session started from an issue: only the reference - the session reads the
 *  description and notes itself through its gitlab_issue tool (piperd serves GitLab); a `gh:owner/repo`
 *  project (W23) names github_issue instead */
export function issueTask(i, project, noteId = null) {
    if (project?.startsWith('gh:')) {
        return [`GitHub issue #${i.iid}: ${i.title}`, i.web_url ?? '', '',
            readLine('github_issue', `repo "${project.slice(3)}", number ${i.iid}`, noteId, false, true),
            'Clarify the task first, then implement and verify.'].join('\n');
    }
    const ref = project ? `project "${project}", iid ${i.iid}` : `iid ${i.iid}`;
    return [`GitLab issue #${i.iid}: ${i.title}`, i.web_url ?? '', '', readLine('gitlab_issue', ref, noteId, false),
        'Clarify the task first, then implement and verify.'].join('\n');
}
/** W9: with a discussion under way the task is its latest comment (`noteId`), the rest only context -
 *  never "analyse the whole issue again"; mirrors piperd's task_for */
function readLine(tool, ref, noteId, mr, gh = false) {
    return noteId
        ? `Handle comment note_${noteId} (and anything newer) - that is the task now. Read it with the tool ${tool} (${ref}). The description and older comments are context only: do not re-analyse the whole ${mr ? (gh ? 'pull request' : 'merge request') : 'issue'}, look back only where the comment needs it.`
        : `Read the task including all comments with the tool ${tool} (${ref}) - the comments are part of the task.`;
}
/** plural of a counted noun (Czech rules; English passes the same word as few and many):
 *  1 → one, 2–4 → few, 0 and 5+ → many (22 → many, as with digits) */
export function plural(n, one, few, many) {
    const a = Math.abs(n);
    return a === 1 ? one : a >= 2 && a <= 4 ? few : many;
}
/** "1 new comment" / "3 new comments"; '' for none */
export function newNotesLabel(n) {
    return n > 0 ? `${n} ${plural(n, 'new comment', 'new comments', 'new comments')}` : '';
}
/** piperd's inbox.ISSUE_FILES: an older piperd sends none */
export const ISSUE_FILES = {
    status: 'status', analysis: 'analysis', comment: 'comment',
    comment_style: 'a short summary on top, technical detail in <details>',
};
/** client-side mirror of piperd's task_for(item, mode, note_id): only references -
 *  the session reads the content itself through gitlab_issue / gitlab_mr. Used to prefill
 *  "Edit task…"; the edited text travels as `task` to POST /api/inbox/start. */
export function taskFor(item, mode, noteId = null, files = ISSUE_FILES) {
    const mr = item.type === 'mr';
    // W23: a GitHub item's gl is "gh:owner/repo" - its tools take repo + number, a PR is #n
    const gh = item.gl.startsWith('gh:');
    const forge = gh ? 'GitHub' : 'GitLab';
    const what = `${forge} ${mr ? (gh ? `pull request #${item.iid}` : `merge request !${item.iid}`) : `issue #${item.iid}`}`;
    const tool = `${gh ? 'github' : 'gitlab'}_${mr ? (gh ? 'pr' : 'mr') : 'issue'}`;
    const ref = gh ? `repo "${item.gl.slice(3)}", number ${item.iid}` : `project "${item.gl}", iid ${item.iid}`;
    const stem = `${gh ? 'github' : 'gitlab'}-${mr ? (gh ? 'pr-' : 'mr-') : ''}${item.iid}`;
    const file = (kind) => `development/${stem}-${files[kind]}.md`;
    const lines = [`${what}: ${item.title}`, item.web_url, '', readLine(tool, ref, noteId, mr, gh)];
    if (mode === 'analyze') {
        lines.push(`Analyze only: change no code, do not commit, send nothing. Clarify the task, find the affected code, propose solution options with a recommendation and an estimate and save it to ${file('analysis')}; in your reply give a short summary and questions for the user.`);
    }
    else {
        lines.push('Clarify the task first, then implement and verify.', `Save a ${forge} comment draft to ${file('comment')} (${files.comment_style}). Do not post it - the user sends it from the GUI.`);
    }
    return lines.join('\n');
}
/** a human reason for the exit code of the sandboxed Claude */
export function exitReason(rc) {
    if (rc === null || rc === undefined)
        return 'reason unknown';
    if (rc === 0)
        return 'Claude finished';
    if (rc === 143 || rc === 130 || rc === 137)
        return `ended ${rc === 137 ? 'forcibly (SIGKILL)' : rc === 130 ? 'by interrupt' : 'by stop'} (code ${rc})`;
    return `ended with an error (code ${rc})`;
}
/** a protected-mounted block as the blocked panel's callout: no approval or exclude unblocks it, so it
 *  says which mount (project / library / include) to change and how */
export function protectedText(b) {
    const what = b.what || 'a directory Piper protects';
    const src = b.source || b.path;
    const writes = b.mode === 'rw';
    const via = b.via === 'project' ? 'the session\u2019s project' : b.via === 'library' ? 'a library' : 'an include (files.include)';
    const rel = src === b.path ? 'it is' : src.startsWith(b.path + '/') ? 'it lies inside' : 'it contains';
    const of = src === b.path ? '' : ` (${b.path})`;
    const where = 'your ~/.hole/settings.json, the project\u2019s .hole/settings.json or its profile';
    const fix = b.via === 'project'
        ? `Fix: start the session on another directory${b.path === src ? ' \u2013 to work on it, use a separate clone outside this checkout' : ''}.`
        : `Fix: remove that ${b.via === 'library' ? 'libraries' : 'files.include'} entry (${where})${writes && b.rw ? ', or make it read-only' : ''}, then Retry.`;
    return {
        title: `Cannot start: the sandbox would ${writes ? 'be able to write' : 'see'} ${what}`,
        what: `${src} is mounted ${b.rw ? 'read-write' : 'read-only'} as ${via} \u2013 ${rel} ${what}${of}. `
            + `A sandbox must not ${writes ? 'write' : 'reach'} it, so this start is refused whatever you approve.`,
        fix,
    };
}
/**
 * The end of an answer as plain text, for the context panel: the last paragraph outside code
 * fences, without Markdown marks. The text may be a tail cut from a longer answer, so an odd
 * number of fences means it starts inside one.
 */
export function lastParagraph(md, max = 400) {
    const lines = md.split('\n');
    let fence = lines.filter((l) => /^\s*(```|~~~)/.test(l)).length % 2 === 1;
    const prose = [];
    for (const l of lines) {
        if (/^\s*(```|~~~)/.test(l)) {
            fence = !fence;
            prose.push('');
        }
        else
            prose.push(fence ? '' : l);
    }
    const paras = prose.join('\n').split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
    const last = (paras[paras.length - 1] ?? '')
        .split('\n').map((l) => l.replace(/^\s*(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/, '').trim()).join(' ')
        .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/(\*\*|__|`)/g, '')
        .replace(/\s+/g, ' ').trim();
    return last.length > max ? last.slice(0, max - 1).trimEnd() + '…' : last;
}
// bidi controls (common.BIDI) and invisible characters (common.INVISIBLE, Unicode tags as surrogate pairs),
// minus U+200D (ZWJ): it joins emoji sequences (👨‍👩‍👧) and reorders nothing - masked, an emoji falls apart
const SHOW_BIDI = /[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u180e\u200b\u200c\u2060-\u2064\ufeff]|\udb40[\udc00-\udc7f]/g;
/** a name the repository chose, shown as it IS (common.show_bidi on cards): a bidi control or an invisible
 *  character becomes a visible [U+202E] - `rtl-‮txt.md` must not read as `rtl-dm.txt` */
export function showBidi(s) {
    return s.replace(SHOW_BIDI, (c) => `[U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`);
}
// only the controls that change the display order (common.BIDI): embeddings/overrides, isolates, marks, ALM
const MARK_BIDI = /[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]/g;
/** every text the GUI puts on screen (dom.ts h(), textContent, title, toasts, notifications): a direction
 *  control becomes a visible [U+202E] so text from a repo, an agent, GitLab or a database can not reorder
 *  what the user reads. Only direction controls - ZWJ and variation selectors stay, or emoji fall apart.
 *  Idempotent, and a no-op on showBidi() output. Not for editable values, the editor, the terminal or the clipboard. */
export function markBidi(s) {
    return s.replace(MARK_BIDI, (c) => `[U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`);
}
/** N9: the text holds a direction control (an editable value keeps it raw - the DB console says so instead) */
export function hasBidi(s) {
    return /[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]/.test(s);
}
/** [start, end) of every case-insensitive occurrence of q in text, as offsets into `text` itself: the offsets
 *  of text.toLowerCase() drift where lowercasing changes the length ('İ' → 'i̇'), and a highlight built
 *  from them marks the wrong characters (N7-2) */
export function matchRanges(text, q) {
    if (!q)
        return [];
    const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&'), 'giu');
    return [...text.matchAll(rx)].filter((m) => m[0]).map((m) => [m.index, m.index + m[0].length]);
}
/** D4: the button / menu label of piper attach - the app it opens in ("Attach in Ghostty"); unknown = the generic one */
export function attachLabel(app) {
    return app ? `Attach in ${app}` : 'Open in terminal';
}
/** Startup is an observed stage, never an estimated percentage or readiness signal. */
/** 9.54: what a start with hole -r does, in the startup panel and the Setup row */
export const REBUILD_HINT = 'hole --rebuild: the agent CLI (Claude Code, Codex) and the apt packages are installed again. Several minutes; running sessions keep their image.';
export function startupStatus(s) {
    // 9.54: a start with hole -r builds the image while hole starts the gateway (minutes): its own stage
    const rb = !!s.rebuild;
    const stages = rb ? ['Prepare', 'Sandbox', 'Network', 'Image', 'Agent', 'Verify'] : ['Prepare', 'Sandbox', 'Network', 'Agent', 'Verify'];
    const late = rb ? 1 : 0; // the stages after Image move one up
    const phases = {
        launch: [1, 'Creating the sandbox', 'Preparing the isolated environment.'],
        hooks: [1, 'Running host setup', 'Host hooks are running. Some steps do not print output.'],
        networks: [2, 'Creating networks', 'Connecting the sandbox services.'],
        cache: [2, 'Starting the image cache', 'Preparing cached container images.'],
        gateway: [2, 'Starting the network gateway', 'Waiting for the sandbox network to be ready.'],
        dind: [2, 'Waiting for Docker', 'The sandbox Docker service is starting and checking its health.'],
        agent: [3 + late, 'Starting the agent', 'Preparing the agent terminal and its connections.'],
        attach: [4 + late, 'Connecting to the agent', 'Finishing startup before the terminal is ready.'],
    };
    if (rb) {
        phases.gateway = [3, 'Rebuilding the sandbox image', REBUILD_HINT];
        phases.dind = [3, 'Waiting for Docker', 'The sandbox Docker service is starting and checking its health.']; // DinD starts after the gateway: never a step back
    }
    let current;
    if (s.state === 'preflight')
        current = [0, 'Checking isolation', s.detail || 'Scanning the project and libraries. Large repositories can take a while.'];
    else if (s.state === 'preparing')
        current = [0, 'Preparing the session', s.detail || 'Preparing settings and files for the sandbox.'];
    else if (s.state === 'attesting')
        current = [4 + late, 'Finishing startup', s.detail || 'Connecting the terminal and checking the sandbox.'];
    else
        current = phases[s.phase] ?? [1, 'Starting the sandbox', 'Waiting for the first startup update.'];
    const since = ['preflight', 'preparing', 'attesting'].includes(s.state) ? s.since : s.phase_since ?? s.since;
    return { stages, step: current[0], title: current[1], hint: current[2], since };
}
/** A local clock keeps moving even when /api/state responds 304 during a quiet start. */
export function startupElapsed(since, now = Date.now() / 1000) {
    const seconds = Number.isFinite(since) ? Math.max(0, Math.floor(now - since)) : 0;
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
}
//# sourceMappingURL=text.js.map