// Files → IDE level, phase 1 core: the pure parts of an open document's life - its state, the save queue, what a
// failed save means and the disk poll's comparison. files.ts wires them in; filedoc.test.mjs pins them.

/** where an open document stands against the file on disk (design a.html #s3 / #s4, report §4.3) */
export type DocState = 'clean' | 'dirty' | 'saving' | 'conflict' | 'missing' | 'readonly';

export interface DocFacts {
  dirty: boolean;
  /** a write of it is in flight */
  saving: boolean;
  /** the file is no longer on disk (deleted / moved / another branch) */
  gone: boolean;
  /** a save was refused because the disk changed, or the disk changed under unsaved text */
  conflict: boolean;
  writable: boolean;
}

/** one state, the most pressing first: a vanished file outranks a conflict, which outranks a pending save */
export function docState(d: DocFacts): DocState {
  if (d.gone) return 'missing';
  if (d.conflict) return 'conflict';
  if (!d.writable) return 'readonly';
  if (d.saving) return 'saving';
  return d.dirty ? 'dirty' : 'clean';
}

/** the status bar's words: what really happens next (S1 - "saves in 2 s" only while the timer runs) */
export function stateText(s: DocState, autosaveSeconds: number): string {
  switch (s) {
    case 'missing': return '⚠ file no longer on disk';
    case 'conflict': return '⚠ conflict with disk';
    case 'readonly': return '🔒 read-only';
    case 'saving': return 'saving…';
    case 'dirty': return autosaveSeconds > 0 ? `● unsaved · saves in ${autosaveSeconds} s · ⌘S now` : '● unsaved · ⌘S save';
    default: return '✎ editable · saved automatically';
  }
}

export type SaveFailureKind = 'conflict' | 'missing' | 'too_large' | 'unsupported' | 'error';

/** what a refused write means, from piperd's typed answer (`code`, 1a). A 409 without a code is not the sha lock
 *  (piperd sends `conflict` for that) but an OS / value error - shown with its own message, like network failures */
export function saveFailure(status: number, code: string): SaveFailureKind {
  if (code === 'conflict' || code === 'missing' || code === 'too_large' || code === 'unsupported') return code;
  if (status === 410) return 'missing';
  if (status === 413) return 'too_large';
  return 'error';
}

/** B4: one write of a document at a time. A save asked for while one runs is queued - several collapse into one
 *  run of the newest job - so it goes out with the sha the first write answered, never the stale one. */
export class SaveQueue {
  private running: Promise<void> | null = null;
  private next: (() => Promise<void>) | null = null;

  get busy(): boolean { return this.running !== null; }

  run(job: () => Promise<void>): Promise<void> {
    this.next = job;
    if (this.running) return this.running;
    this.running = (async () => {
      try {
        while (this.next) {
          const j = this.next;
          this.next = null;
          try { await j(); } catch { /* the job reports its own failure */ }
        }
      } finally { this.running = null; }
    })();
    return this.running;
  }
}

/** the disk poll (1b): what moved since the last look. `known` is the token of the shown text ('' = known to be
 *  gone, undefined = unknown: read it once to learn it); `now` is /api/fs/stat's answer (null = gone / not visible). */
export function movedPaths(known: (path: string) => string | undefined, now: Record<string, string | null>): { changed: string[]; gone: string[]; back: string[] } {
  const out = { changed: [] as string[], gone: [] as string[], back: [] as string[] };
  for (const [path, ver] of Object.entries(now)) {
    const was = known(path);
    if (ver === null) { if (was !== '') out.gone.push(path); }
    else if (was === '') out.back.push(path);
    else if (ver !== was) out.changed.push(path);
  }
  return out;
}

/** the poll's cadence while Files is shown (report §5 1b: 2–3 s) */
export const POLL_MS = 2500;
