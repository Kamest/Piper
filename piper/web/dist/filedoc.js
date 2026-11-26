// Files → IDE level, phase 1 core: the pure parts of an open document's life - its state, the save queue, what a
// failed save means and the disk poll's comparison. files.ts wires them in; filedoc.test.mjs pins them.
/** one state, the most pressing first: a vanished file outranks a conflict, which outranks a pending save */
export function docState(d) {
    if (d.gone)
        return 'missing';
    if (d.conflict)
        return 'conflict';
    if (!d.writable)
        return 'readonly';
    if (d.saving)
        return 'saving';
    return d.dirty ? 'dirty' : 'clean';
}
/** the status bar's words: what really happens next (S1 - "saves in 2 s" only while the timer runs) */
export function stateText(s, autosaveSeconds) {
    switch (s) {
        case 'missing': return '⚠ file no longer on disk';
        case 'conflict': return '⚠ conflict with disk';
        case 'readonly': return '🔒 read-only';
        case 'saving': return 'saving…';
        case 'dirty': return autosaveSeconds > 0 ? `● unsaved · saves in ${autosaveSeconds} s · ⌘S now` : '● unsaved · ⌘S save';
        default: return '✎ editable · saved automatically';
    }
}
/** what a refused write means, from piperd's typed answer (`code`, 1a); an older 409 without a code is the conflict
 *  it always was, any other status a plain error (network, permission, a working session) */
export function saveFailure(status, code) {
    if (code === 'conflict' || code === 'missing' || code === 'too_large' || code === 'unsupported')
        return code;
    if (status === 410)
        return 'missing';
    if (status === 413)
        return 'too_large';
    if (status === 409 && !code)
        return 'conflict';
    return 'error';
}
/** B4: one write of a document at a time. A save asked for while one runs is queued - several collapse into one
 *  run of the newest job - so it goes out with the sha the first write answered, never the stale one. */
export class SaveQueue {
    running = null;
    next = null;
    get busy() { return this.running !== null; }
    run(job) {
        this.next = job;
        if (this.running)
            return this.running;
        this.running = (async () => {
            try {
                while (this.next) {
                    const j = this.next;
                    this.next = null;
                    try {
                        await j();
                    }
                    catch { /* the job reports its own failure */ }
                }
            }
            finally {
                this.running = null;
            }
        })();
        return this.running;
    }
}
/** the disk poll (1b): what moved since the last look. `known` is the token of the shown text ('' = known to be
 *  gone, undefined = unknown: read it once to learn it); `now` is /api/fs/stat's answer (null = gone / not visible). */
export function movedPaths(known, now) {
    const out = { changed: [], gone: [], back: [] };
    for (const [path, ver] of Object.entries(now)) {
        const was = known(path);
        if (ver === null) {
            if (was !== '')
                out.gone.push(path);
        }
        else if (was === '')
            out.back.push(path);
        else if (ver !== was)
            out.changed.push(path);
    }
    return out;
}
/** the poll's cadence while Files is shown (report §5 1b: 2–3 s) */
export const POLL_MS = 2500;
//# sourceMappingURL=filedoc.js.map