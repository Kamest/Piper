// Session helpers of the Overview and the audit timeline (9.6 #8).
// GET /api/board (one card per session: the last prompt and answer, changes, tokens, the issue
// workflow) feeds the queue's second lines and the detail's header and context; the pieces
// here are the workflow strip and the issue status button shown there.
// The timeline is $PIPER_HOME/audit.log, newest first, optionally of one session.

import { api } from './api.js';
import { clear, fi, h } from './dom.js';
import { flowSteps } from './inbox.js';
import { cutText, showBidi, markBidi } from './text.js';
import type { AuditEvent, BoardFlow, IssueStateCard, Session } from './types.js';

const NEEDS_YOU = ['attention', 'trust', 'waiting', 'blocked', 'error'];
const RUNNING = ['working', 'ready', 'starting', 'attesting', 'spawning', 'preparing', 'preflight', 'stopping'];

/** the workflow strip of a card (9.8 #7): done steps ticked, the next one emphasized */
export function flowStrip(f: BoardFlow): HTMLElement {
  const strip = h('ol', { class: 'cflow', 'aria-label': 'issue workflow' });
  for (const st of flowSteps(f)) {
    const mark = st.state === 'done' ? '✓ ' : st.partial ? '◐ ' : '';
    strip.append(h('li', { class: `fstep ${st.state}${st.partial ? ' partial' : ''}`, title: st.title }, mark + st.label));
  }
  return strip;
}

/** the workflow as one bar for the context panel: a segment per step (title = what it means) and
 *  one line of words - how far, what is next, what was skipped */
export function flowBar(f: BoardFlow): HTMLElement {
  const steps = flowSteps(f);
  const bar = h('ol', { class: 'fbar', 'aria-label': 'issue workflow' }, ...steps.map((st) =>
    h('li', { class: `${st.state}${st.partial ? ' partial' : ''}`, title: st.title, 'aria-label': `${st.label}: ${st.state}` })));
  const done = steps.filter((x) => x.state === 'done').length;
  const next = steps.find((x) => x.state === 'next');
  const skipped = steps.filter((x) => x.state === 'skipped').map((x) => x.label);
  const words = h('p', { class: 'fwords' }, `${done} of ${steps.length} done`);
  if (next) words.append(' · next: ', h('b', {}, next.label));
  if (skipped.length) words.append(markBidi(` · ${skipped.join(', ')} skipped`));
  return h('div', { class: 'fbox' }, bar, words);
}

/** 9.12 D: "Update issue status" - a live session gets one prompt, an ended one piperd's
 * summary of its last turns in the issue's status file (the Edit guard applies) */
export function istateButton(s: Session, ist: IssueStateCard): HTMLElement {
  const live = ist.prompt;   // piperd's rule: Claude sits at its prompt (a live session otherwise waits for that)
  const label = ist.exists ? 'Update issue status' : 'Create issue status';
  const btn = h('button', {
    class: 'link small cistate',
    title: live ? `Claude writes the status to ${ist.path}` : `piperd writes a summary of the last turns and the facts from git to ${ist.path}`,
  }, label) as HTMLButtonElement;
  btn.onclick = async (e: Event) => {
    e.stopPropagation();
    btn.disabled = true;
    try {
      const r = (await api.issueState(s.id)).data;
      btn.textContent = markBidi(r?.sent ? '✓ Status update requested' : `✓ Written to ${r?.written ?? ist.path}`);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = markBidi(`${label} - failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  return btn;
}

export function section(s: Session): 'wait' | 'run' | 'done' {
  return NEEDS_YOU.includes(s.state) ? 'wait' : RUNNING.includes(s.state) ? 'run' : 'done';
}

/** the human's last prompt for a card line: a paste becomes a marker, never the wrapper tag */
export function askedText(t: string): string {
  // only the paste wrapper: `List<String>`, `<details>` and XML in a prompt are the human's text
  return t.replace(/<pasted_content\b[^>]*>[\s\S]*?(<\/pasted_content>|$)/g, '[pasted text] ').trim();
}

export function fmtK(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n);
}

const SKIP_KEYS = new Set(['event', 'ts', 'session']);

/** D16: the audit's event names as words; anything else falls back to the raw name without the dashes */
const AUDIT_NAMES: Record<string, string> = {
  'session-create': 'Session created', spawn: 'Sandbox started', attest: 'Sandbox attested', preflight: 'Preflight',
  exit: 'Session ended', confirm: 'Confirmed', tool: 'Tool call', 'trust-prompt': 'Trust prompt', unlock: 'Unlocked',
  'stack-grant': 'Stack access granted', 'stack-revoke-failed': 'Stack access not revoked', 'allow-domain': 'Domain allowed',
  'settings-change': 'Settings changed', 'settings-approved': 'Settings approved', 'settings-by-session': 'Settings changed by the session',
  'settings-repaired': 'Settings repaired', 'features-change': 'Modules changed', 'features-init': 'Modules set up',
  'features-migrate': 'Modules migrated', 'git-run': 'Git', 'git-push': 'Git push', 'git-commit': 'Git commit',
  'git-branch': 'Git branch', 'git-worktree': 'Git worktree', 'git-guard': 'Git refused', 'gui-send': 'Sent to the session',
  'gui-write': 'File saved', 'gui-delete': 'File deleted', 'gui-create': 'File created', 'gui-rename': 'File renamed',
  'gui-read-hidden': 'Hidden file read', 'gui-mr': 'Merge request', 'gui-pr': 'Pull request', 'gui-comment': 'Comment posted',
  'http-run': 'HTTP request', db: 'Database query', 'host-terminal-open': 'Host shell opened', 'host-terminal-close': 'Host shell closed',
  'host-terminal-exit': 'Host shell ended', 'host-terminal-refused': 'Host shell refused', 'tty-attach': 'Terminal attached',
  'open-terminal': 'Attached in a terminal', rename: 'Renamed', 'stop-nudge': 'Stop repeated', 'kill-failed': 'Stop failed',
};

/** D16: piperd's own housekeeping - shown dimmed under All, hidden under Actions */
export const AUDIT_INTERNAL = new Set(['attest', 'spawn', 'preflight', 'stack-grant', 'stack-revoke-failed', 'features-init',
  'features-migrate', 'gui-restore', 'login-collected', 'template-synced', 'template-sync-failed', 'codex-template-synced',
  'codex-template-sync-failed', 'maven-settings', 'maven-settings-failed', 'home-migrated', 'settings-repaired', 'stop-nudge',
  'gui-write-recovered', 'mm-deferred', 'mm-sent', 'mm-dropped', 'chat-deliver', 'resume-dropped', 'repo-root-unrecorded',
  'agent-log-failed', 'db-reap-error', 'gitlab-read', 'inbox-auto-skipped', 'gui-inbox-seen', 'gui-inbox-unseen',
  'gui-inbox-refresh', 'db-drivers-installed', 'ijhttp-install']);

/** the readable name of an audit event (the raw one stays in the row's tooltip and in the filter) */
export function auditLabel(event: string): string {
  const s = AUDIT_NAMES[event] ?? event.replace(/^gui-/, '').replace(/-/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** a long command line in a row: the program and its verb, the rest as a count (the full one on a click) */
const ARGV_SHORT = 4;

/** one line of detail per audit record, long values cut; `short` folds a long argv (D16) -
 *  never a host git run's args, they are the record itself (N6) */
export function auditDetail(e: AuditEvent, short = false): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(e)) {
    if (SKIP_KEYS.has(k) || v === null || v === undefined || v === '') continue;
    // a long list (keys, rules) says more as a count than as 2 kB of JSON; a host git run's argv is the
    // record itself (N6: "args: 4 items" hid `push --force origin main`)
    let text = typeof v === 'string' ? v : Array.isArray(v) && v.length > 3 && !(k === 'args' && e.event === 'git-run')
      ? `${v.length} items` : JSON.stringify(v);
    if (k === 'argv' && Array.isArray(v)) {
      const argv = (v as string[]).filter((x) => !x.includes('\n'));
      text = short && argv.length > ARGV_SHORT ? `${argv.slice(0, 2).join(' ')} … +${argv.length - 2}` : argv.join(' ');
    }
    if (text.length > 240) text = cutText(text, 240) + '…';
    parts.push(`${k}: ${text}`);
  }
  // a file name / git argument / SQL with a bidi control (U+202E) would reverse the rest of the row
  return showBidi(parts.join(' · '));
}

const str = (v: unknown): string => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v));
const base = (p: string): string => p.split('/').pop() || p;

/** F20: what one action did, as a sentence - "Push HEAD → release", "git show --stat 4d890c0",
 *  "Hidden file read · settings.properties"; the raw fields stay in auditDetail (the unfolded row) */
export function auditSentence(e: AuditEvent): string {
  const args = Array.isArray(e.args) ? (e.args as unknown[]).map(str) : [];
  let s: string;
  switch (e.event) {
    case 'turn-revert': s = e.status === 'saved' ? 'Saved files before revert · Undo available'
      : Number(e.turn) ? `Reverted files to turn ${str(e.turn)}` : 'Restored saved files (Undo revert)'; break;
    case 'git-run': {
      if (args[0] === 'push') {
        const ref = args.slice(1).filter((a) => !a.startsWith('-')).pop() ?? '';
        const [src, dst] = ref.includes(':') ? ref.split(':', 2) : [ref, ''];
        const force = args.some((a) => a === '--force' || a === '-f' || a.startsWith('--force-with-lease') || ref.startsWith('+'));
        s = `Push ${src}${dst ? ` → ${dst}` : ''}${force ? ' (force)' : ''}`;
      } else s = cutText(`git ${args.join(' ')}`, 160);
      if (e.rc !== undefined && e.rc !== 0) s += ` - failed (rc ${str(e.rc)})`;
      break;
    }
    case 'git-push': s = `Push ${str(e.branch)}`; break;
    case 'git-commit': {
      const n = Array.isArray(e.files) ? e.files.length : 0;
      s = `Commit ${str(e.commit).slice(0, 7)}${n ? ` · ${n} ${n === 1 ? 'file' : 'files'}` : ''}${e.amend ? ' (amend)' : ''}`;
      break;
    }
    case 'gui-read-hidden': s = `Hidden file read · ${base(str(e.path))}`; break;
    case 'gui-write': case 'gui-delete': case 'gui-create':
      s = `${auditLabel(e.event)} · ${base(str(e.path ?? e.file))}`; break;
    case 'http-run': s = `HTTP ${str(e.file)} on ${str(e.env) || 'no env'}${e.ok === false ? ' - failed' : ''}`; break;
    case 'db': s = `Database ${str(e.tool).replace(/^db_?/, '') || 'query'} on ${str(e.connection)}${e.ok === false ? ' - failed' : ''}`; break;
    case 'tool': s = `Tool ${str(e.tool)}${e.ok === false ? ` - failed${e.error ? `: ${str(e.error)}` : ''}` : ''}`; break;
    case 'confirm': s = `Card · ${str(e.kind) || 'action'} (${confirmWord(e)})`; break;
    case 'session-create': s = `Session created${e.origin ? ` (${str(e.origin)})` : ''}`; break;
    case 'exit': s = `Session ended${e.rc !== undefined && e.rc !== 0 ? ` (rc ${str(e.rc)})` : ''}`; break;
    case 'gui-send': s = `Sent to the session${e.chars ? ` (${str(e.chars)} chars)` : ''}`; break;
    case 'gui-mr': s = `Merge request${e.iid ? ` !${str(e.iid)}` : ''} ${str(e.source)} → ${str(e.target)}`; break;
    default: s = auditLabel(e.event);
  }
  return showBidi(cutText(s, 200));
}

/** F20: a card's answer as a word after the action ("auto-allowed", "declined") */
export function confirmWord(c: AuditEvent): string {
  if (c.auto) return 'auto-allowed';
  if (c.approved) return c.always ? 'always allowed' : 'approved';
  return c.cancelled ? 'cancelled' : c.answered === false ? 'timed out' : 'declined';
}

/** F20: only risky actions get a colour - a push and production; the rest is grey */
export function auditRisk(e: AuditEvent): 'push' | 'prod' | '' {
  if (e.event === 'git-push' || (e.event === 'git-run' && Array.isArray(e.args) && e.args[0] === 'push')) return 'push';
  if (e.cls === 'prod' || e.class === 'prod' || e.env === 'prod') return 'prod';
  return '';
}

/** the line icon of one record (`fi-<name>`); 'dot' is a plain dot, larger and red for production */
export function auditIcon(e: AuditEvent): 'up' | 'prod' | 'branch' | 'eye' | 'ask' | 'dot' {
  const r = auditRisk(e);
  if (r === 'push') return 'up';
  if (r === 'prod') return 'prod';
  if (e.event.startsWith('git-')) return 'branch';
  if (e.event === 'gui-read-hidden') return 'eye';
  if (e.event === 'confirm') return 'ask';
  return 'dot';
}

function auditIconEl(e: AuditEvent): HTMLElement {
  const name = auditIcon(e);
  return name === 'dot' || name === 'prod' ? h('i', { class: 'adot' }) : h('i', { class: `fi fi-${name}` });
}

/** the tool / card / record of one call share this key: git_run ↔ git-run */
function actionKey(e: AuditEvent): string {
  return e.event === 'tool' ? str(e.tool) : e.event === 'confirm' ? str(e.kind) : e.event.replace(/-/g, '_');
}

const tsMs = (ts: string): number => {
  const m = Date.parse(ts.replace(/([+-]\d\d)(\d\d)$/, '$1:$2'));
  return Number.isNaN(m) ? 0 : m;
};

export interface AuditRow {
  /** the record the row is about (the git run, not its Tool call / Confirmed) */
  e: AuditEvent;
  /** every record of the row: the parts of one action, then the repeats */
  parts: AuditEvent[];
  /** the card's answer for this action, if one was audited */
  confirm: AuditEvent | null;
  /** how many times the same action came in a row; `first` = the oldest ts */
  n: number;
  first: string;
}

/** F20: one row per action - newest first, the Tool call · Git · Confirmed records of one call (same session, same
 *  tool, within 5 s) folded into one; the same action repeated in a row becomes one row with ×n */
export function mergeAudit(events: AuditEvent[]): AuditRow[] {
  const acts: AuditRow[] = [];
  for (let i = 0; i < events.length; i++) {
    const x = events[i]!;
    const group = [x];
    const key = actionKey(x);
    if (x.session && key) {
      for (let j = i + 1; j < events.length; j++) {
        const y = events[j]!;
        if (y.session !== x.session || actionKey(y) !== key || Math.abs(tsMs(x.ts) - tsMs(y.ts)) > 5000 || group.some((g) => g.event === y.event)) break;
        group.push(y);
      }
    }
    i += group.length - 1;
    const main = group.find((g) => g.event !== 'tool' && g.event !== 'confirm') ?? group.find((g) => g.event === 'tool') ?? x;
    const confirm = main.event !== 'confirm' ? group.find((g) => g.event === 'confirm') ?? null : null;
    acts.push({ e: main, parts: group, confirm, n: 1, first: main.ts });
  }
  const out: AuditRow[] = [];
  const sig = (r: AuditRow) => `${r.e.event}|${auditSentence(r.e)}|${str(r.e.project)}|${r.e.session ?? ''}|${r.confirm ? confirmWord(r.confirm) : ''}`;
  for (const r of acts) {
    const prev = out[out.length - 1];
    if (prev && r.e.event !== 'turn-revert' && sig(prev) === sig(r)) {
      prev.n++;
      prev.first = r.e.ts;
      prev.parts.push(...r.parts);
    } else out.push(r);
  }
  return out;
}

/** "×10 in 21 min" (the span between the first and the last) */
export function repeatText(n: number, first: string, last: string): string {
  const min = Math.round(Math.abs(tsMs(last) - tsMs(first)) / 60000);
  return `×${n}${min ? ` in ${min < 120 ? `${min} min` : `${Math.round(min / 60)} h`}` : ''}`;
}

/** the time of a row: "23:34" today, "Oct 19 23:34" before; a run of repeats as "23:34–23:55" */
export function auditTime(ts: string, today = new Date()): string {
  const d = new Date(tsMs(ts));
  if (!tsMs(ts)) return ts;
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return d.toDateString() === today.toDateString() ? hm : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${hm}`;
}

/** N9: the line under the audit table - a filter that hides every record used to leave a blank page */
export function auditEmptyLine(total: number, shown: number): string {
  return !total ? 'No records.' : !shown ? 'Nothing matches the filter.' : '';
}

export interface AuditDeps {
  titleOf(id: string): string;
  select(id: string): void;
  undoRevert?(id: string, undo: string): void;
}

export function createAudit(deps: AuditDeps) {
  const el = h('div', { class: 'audit' });
  let session = '';
  let filter = '';
  let events: AuditEvent[] = [];
  let actionsOnly = true;   // D16: the user's actions by default; All shows piperd's housekeeping too (nothing is deleted)
  const open = new Set<string>();   // F20: rows unfolded to their raw records (key: ts|event|session)
  const input = h('input', { type: 'search', placeholder: 'Filter events', autocomplete: 'off' }) as HTMLInputElement;
  input.addEventListener('input', () => { filter = input.value.toLowerCase(); draw(); });
  const title = h('h2', {});
  const body = h('div', { class: 'events' });
  const mode = h('div', { class: 'amode', role: 'group', 'aria-label': 'records' });
  const drawMode = () => {
    clear(mode);
    for (const [only, label, tip] of [[true, 'Actions', 'without piperd\'s housekeeping (sandbox start, attest, stack grants, syncs)'], [false, 'All', 'every record']] as const) {
      mode.append(h('button', { class: actionsOnly === only ? 'on' : '', 'aria-pressed': String(actionsOnly === only), title: tip,
        onclick: () => { actionsOnly = only; drawMode(); draw(); } }, label));
    }
  };
  drawMode();
  el.append(h('div', { class: 'bhead' }, title, mode, input, h('button', { class: 'icon', title: 'load again', 'aria-label': 'load again', onclick: () => void load() }, fi('refresh'))), body);

  async function load(): Promise<void> {
    open.clear();
    try {
      events = (await api.audit(session, session ? 500 : 300)).data?.events ?? [];
    } catch (e) {
      events = [{ event: 'error', ts: '', detail: String(e instanceof Error ? e.message : e) }];
    }
    draw();
  }

  function draw(): void {
    // the page header names the view (Audit / the session's History tab): no second title here
    title.textContent = '';
    title.classList.add('hidden');
    clear(body);
    const list = h('div', { class: 'arows', role: 'list' });
    let shown = 0;
    const visible = events.filter((e) => !(actionsOnly && AUDIT_INTERNAL.has(e.event)));
    for (const r of mergeAudit(visible)) {
      const e = r.e;
      const internal = AUDIT_INTERNAL.has(e.event);
      const sentence = auditSentence(e) + (r.confirm ? ` (${confirmWord(r.confirm)})` : '');
      const raw = r.parts.map((p) => `${auditLabel(p.event)} · ${auditDetail(p)}`);
      if (filter && !`${r.parts.map((p) => p.event).join(' ')} ${sentence} ${raw.join(' ')}`.toLowerCase().includes(filter)) continue;
      shown++;
      const key = `${e.ts}|${e.event}|${e.session ?? ''}`;
      const isOpen = open.has(key);
      const risk = auditRisk(e);
      const answer = r.confirm ?? (e.event === 'confirm' ? e : null);
      const declined = !!answer && !answer.approved;
      const time = r.n > 1 ? `${auditTime(r.first)}–${auditTime(e.ts).split(' ').pop()}` : auditTime(e.ts);
      const where = !session && e.session
        ? h('button', { class: 'link aproj', title: deps.titleOf(e.session), onclick: (ev: Event) => { ev.stopPropagation(); deps.select(e.session!); } },
          markBidi(str(e.project) || deps.titleOf(e.session)))
        : h('span', { class: 'aproj' }, markBidi(str(e.project)));
      const row = h('div', { class: `arow ev-${e.event}${internal ? ' internal' : ''}${risk ? ` risk-${risk}` : ''}${declined ? ' declined' : ''}${isOpen ? ' open' : ''}`,
        role: 'listitem', tabindex: '0', 'aria-expanded': String(isOpen), title: `${e.event} · ${e.ts}` },
        h('span', { class: 'atime mono' }, markBidi(time)),
        h('span', { class: 'aicon', 'aria-hidden': 'true' }, auditIconEl(e)),
        h('span', { class: 'atext' }, sentence, r.n > 1 ? h('span', { class: 'arep', title: `${r.n} times in a row` }, repeatText(r.n, r.first, e.ts)) : null),
        where);
      if (e.event === 'turn-revert' && e.session && typeof e.undo === 'string' && deps.undoRevert) {
        row.append(h('button', { class: 'mini', onclick: () => deps.undoRevert?.(e.session!, String(e.undo)) }, 'Undo revert'));
      }
      const toggle = (ev: Event) => {
        if ((ev.target as HTMLElement).closest('button') || String(getSelection() ?? '')) return;   // a session link / selecting text
        if (!open.delete(key)) open.add(key);
        draw();
      };
      row.addEventListener('click', toggle);
      row.addEventListener('keydown', (ev: KeyboardEvent) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(ev); } });
      list.append(row);
      if (isOpen) {
        // the raw records: every part of the action, every repeat with its time
        list.append(h('div', { class: 'araw mono small' }, ...r.parts.map((p) => h('div', {}, h('span', { class: 'mut' }, `${p.ts.replace('T', ' ').slice(0, 19)}  `), markBidi(`${auditLabel(p.event)} · ${auditDetail(p)}`)))));
      }
    }
    body.append(list);
    const empty = auditEmptyLine(events.length, shown);
    // pages polish: an empty list says what to do next - load again, clear the filter, or show the housekeeping too
    if (empty) body.append(h('p', { class: 'mut aempty' }, empty, ' ',
      !events.length ? h('button', { class: 'link', onclick: () => void load() }, 'Load again')
        : filter ? h('button', { class: 'link', onclick: () => { input.value = filter = ''; draw(); } }, 'Clear the filter')
          : h('button', { class: 'link', onclick: () => { actionsOnly = false; drawMode(); draw(); } }, 'Show all records')));
  }

  return {
    el,
    show(id = ''): void {
      session = id;
      draw();
      void load();
    },
  };
}
