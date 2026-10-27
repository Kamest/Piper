// Claude's Quiet UX refinement: stable owner context, truthful reports, durable activity.
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';
import type { QuietParticipant, QuietRequest, QuietSummary, Session } from './types.js';

export const QUIET_SCOPE = 'Piper only asks its own sessions to wait. Apps and processes outside Piper may keep running.';
export const quietText = (v: unknown, fallback = ''): string => typeof v === 'string' && v.trim() && !['null', 'undefined', 'NaN'].includes(v.trim()) ? v : fallback;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0;
export const quietTime = (v: unknown): string => finite(v) && Number.isFinite(new Date(v * 1000).getTime()) ? new Date(v * 1000).toLocaleTimeString() : 'Not recorded';
export const quietDuration = (v: unknown): string => v == null ? 'Until released' : finite(v) ? `Time limit: ${v} min` : 'Duration unavailable';
const ago = (at: unknown, now: number): string => !finite(at) ? 'Not recorded' : Math.max(0, now - at) < 60 ? `${Math.floor(Math.max(0, now - at))} s ago` : `${Math.floor(Math.max(0, now - at) / 60)} min ago`;
const nameOf = (r: { title?: string; id?: string } | null | undefined) => quietText(r?.title, quietText(r?.id, 'Session'));
const partsOf = (a?: QuietRequest | null) => Array.isArray(a?.participants) ? a.participants! : [];
const names = (parts: QuietParticipant[]) => parts.length > 2 ? `${parts.length} sessions` : parts.map(p => nameOf(p)).join(' and ');
/** Sessions the operator let keep working (allowed) or that are returning from it: either makes the window shared. */
export const quietExempt = (a: QuietRequest | null | undefined, state: 'allowed' | 'returning') => partsOf(a).filter(p => p.exempt === state);
export function quietStatus(a: QuietRequest | null | undefined): { label: string; title: string; tone: string; step: number } {
  if (!a) return { label: 'Window released', title: 'Window released.', tone: 'mut', step: 4 };
  const held = partsOf(a).filter(p => p.exempt !== 'allowed'), n = held.filter(p => p.status === 'waiting').length, owner = nameOf(a);
  const allowed = quietExempt(a, 'allowed'), returning = quietExempt(a, 'returning');
  if (['draining', 'settling'].includes(a.phase)) return { label: `Asking ${held.length} sessions to pause · ${n} of ${held.length} waiting`, title: partsOf(a).some(p => p.stale) ? 'Still waiting for other sessions.' : 'Waiting for other sessions.', tone: 'warn', step: 1 };
  if (['ready', 'working'].includes(a.phase) && allowed.length) return { label: `Shared · ${owner} can work, ${names(allowed)} ${allowed.length === 1 ? 'keeps' : 'keep'} working`, title: 'This window is shared, not quiet.', tone: 'warn', step: a.phase === 'ready' ? 2 : 3 };
  if (['ready', 'working'].includes(a.phase) && returning.length) return { label: `Shared · ${names(returning)} ${returning.length === 1 ? 'is' : 'are'} stopping ${returning.length === 1 ? 'its' : 'their'} work`, title: 'This window stays shared until they confirm.', tone: 'warn', step: a.phase === 'ready' ? 2 : 3 };
  if (a.phase === 'ready') return { label: `Quiet · ${owner} can start work`, title: 'Your quiet window is ready.', tone: 'ok', step: 2 };
  if (a.phase === 'working') return { label: `${owner} reports work is running`, title: `${owner} reports work is running.`, tone: 'ok', step: 3 };
  return { label: a.ready ? `Piper can’t confirm ${owner} is still working` : 'Piper lost track before your window started.', title: a.ready ? 'Check the work before continuing.' : 'Piper lost track before your window started.', tone: 'warn', step: a.ready ? 3 : 1 };
}
export function quietParticipant(p: QuietParticipant, a: QuietRequest | null, _step = 0, now = Date.now() / 1000) {
  const source = quietText(p.confirmed_by, 'agent report');
  if (!a) return p.resume?.status === 'sent'
    ? { label: 'Notice delivered', tone: 'mut', detail: quietText(p.resume.detail, 'Piper delivered the continue notice'), source: 'Piper observed' }
    : { label: 'Notice pending', tone: 'warn', detail: quietText(p.resume?.detail, 'Piper will retry the continue notice'), source: 'Piper observed' };
  if (p.exempt === 'allowed') return { label: 'Exception', tone: 'warn', detail: `Keeps working by your choice${finite(p.at) ? ` · since ${quietTime(p.at)}` : ''}`, source: 'operator' };
  if (p.exempt === 'returning' && p.status !== 'unknown') return { label: 'Stopping', tone: 'warn', detail: `Exception removed · ${p.confirmed_by === 'operator' ? 'must stop and confirm again' : quietText(p.detail, 'stopping its work')}`, source };
  if (p.status === 'unknown') return { label: 'Needs checking', tone: 'warn', detail: quietText(p.detail, 'Session ended or restarted. Not confirmed.'), source: 'Piper observed' };
  if (p.status === 'waiting') return { label: 'Waiting', tone: 'mut', detail: `${quietText(p.detail, 'Confirmed waiting')}${finite(p.at) ? ` · ${quietTime(p.at)}` : ''}`, source };
  const serverNow = finite(a.now) ? a.now : now, at = p.at ?? p.delivered_at ?? a.selected;
  const stale = p.stale ?? (finite(at) && serverNow - at >= 120);
  const age = ago(stale && finite(at) ? Math.min(at, serverNow - 120) : at, serverNow);
  if (finite(p.at)) return { label: stale ? 'No update' : 'Finishing', tone: 'warn', detail: `${quietText(p.detail, quietText(p.activity, 'Finishing current work'))} · ${stale ? 'last update' : 'updated'} ${age}`, source };
  if (finite(p.delivered_at)) return { label: stale ? 'No response' : 'Request sent', tone: stale ? 'warn' : 'mut', detail: stale ? `Request sent ${age} · no reply yet` : 'Waiting for the first reply', source: 'Piper observed' };
  return { label: 'Delivering', tone: 'mut', detail: quietText(p.delivery_detail, 'Request not delivered yet'), source: 'Piper observed' };
}
export function quietBadge(summary: QuietSummary | null | undefined, sid: string): string {
  const a = summary?.active;
  if (!a) return '';
  if (a.owner === sid) return 'Owner';
  if (summary?.queue?.some(q => q.owner === sid)) return 'Queued';
  const p = partsOf(a).find(x => x.id === sid);
  return p ? quietParticipant(p, a).label : '';
}
export function quietOwner(active: QuietRequest | null | undefined, last: QuietRequest | null | undefined, sessions: Session[]) {
  const request = active ?? last;
  if (!request) return null;
  const s = sessions.find(s => s.id === request.owner);
  return { id: request.owner, name: quietText(s?.title, nameOf({ ...request, id: request.owner })), meta: s?.agent === 'codex' ? 'Codex' : s?.agent === 'claude' ? 'Claude' : '',
    reason: quietText(request.reason), role: active ? 'Owner' : 'Last owner', tag: !s ? 'Removed' : ['exited', 'error'].includes(s.state) ? 'Ended' : '', canOpen: !!s };
}
interface Deps {
  state(): QuietSummary | null | undefined; sessions(): Session[]; seen(): boolean; markSeen(): void;
  act(body: Record<string, unknown>): Promise<void>; open(sid: string): void; close(): void;
  confirm(title: string, text: string, run: () => void, label?: string): void;
  error(message: string): void; changed(): void; outdated?(): boolean; reload?(): void;
}
const EVENT_LABELS: Record<string, string> = { exempt_granted: 'Exception granted', exempt_revoked: 'Exception removed', requested: 'Requested a quiet window', queued: 'Joined the queue', draining: 'Preparing requests to other sessions', settling: 'All confirmed · checking Piper jobs', ready: 'Window quiet · owner can start work', working: 'Owner reported work started', recovering: 'Needs attention', released: 'Released the window', cancelled: 'Cancelled the request', skipped: 'Request skipped', delivered: 'Request delivered', progress: 'Reported finishing', waiting: 'Confirmed waiting', unknown: 'Session needs checking', resume_sent: 'Continue notice delivered', resume_pending: 'Continue notice pending', job_running: 'Piper job running', job_ended: 'Piper job ended', work_ended: 'Owner reported work ended', participant_added: 'Session joined this window', manual_renew: 'Operator confirmed the window is still needed' };
export function createQuiet(deps: Deps) {
  const el = h('section', { class: 'view quiet-view', 'aria-label': 'Quiet window' });
  let selected = '', mode: 'intro' | 'request' | 'state' = 'state', helpReturn: 'intro' | 'request' | 'state' = 'state', signature = '', details = false, technical = false, allEvents = false, busy = false, reason = '', returnable = false;
  const buttons = (...nodes: (HTMLElement | null)[]) => h('div', { class: 'quiet-actions' }, ...nodes);
  const btn = (label: string, run: () => void, primary = false) => h('button', { type: 'button', class: primary ? 'primary' : '', 'data-quiet-focus': label, onclick: run, disabled: busy }, label);
  const scope = () => h('p', { class: 'quiet-scope' }, QUIET_SCOPE);
  const now = () => finite(deps.state()?.active?.now) ? deps.state()!.active!.now! : Date.now() / 1000;
  function saveDraft() { try { sessionStorage.setItem('piper.quiet.draft', JSON.stringify({ selected, reason, mode: mode === 'intro' ? helpReturn : mode })); } catch { /* storage unavailable */ } }
  function discardDraft() { returnable = false; try { sessionStorage.removeItem('piper.quiet.draft'); } catch { /* unavailable */ } }
  function show() { signature = ''; update(); deps.changed(); }
  async function action(body: Record<string, unknown>) {
    if (busy) return;
    busy = true; show();
    try { await deps.act(body); if (body.action === 'request') discardDraft(); mode = 'state'; }
    catch (e) { deps.error(e instanceof Error ? e.message : String(e)); }
    finally { busy = false; show(); }
  }
  function ownerCard(r: QuietRequest, role: string, primary = false) {
    const owner = quietOwner(r, null, deps.sessions())!;
    const check = r.phase !== 'queued' && r.phase !== 'released' && finite(r.lease_until) ? r.lease_until - 90 : null;
    return h('div', { class: 'quiet-owner', 'data-quiet-owner': owner.id },
      h('div', { class: 'quiet-owner-text' }, h('div', { class: 'quiet-owner-line' }, h('span', { class: 'quiet-owner-role' }, role),
        h('strong', { class: 'quiet-owner-name', title: owner.name }, owner.name), owner.meta ? h('span', { class: 'quiet-owner-meta' }, owner.meta) : null,
        owner.tag ? h('span', { class: 'chip quiet-owner-tag' }, owner.tag) : null),
        h('div', { class: 'quiet-owner-reason', title: owner.reason }, owner.reason,
          check ? h('span', { 'data-quiet-check': String(check) }, ` · last check-in ${ago(check, now())}`) : null)),
      owner.canOpen ? h('button', { type: 'button', class: primary ? 'primary' : 'ghost', 'aria-label': `Open session ${owner.name}`, title: owner.name,
        onclick: () => { saveDraft(); deps.open(owner.id); } }, owner.tag === 'Ended' ? 'Open to inspect' : `Open ${owner.name}`) : null);
  }
  function requestingCard() {
    const s = deps.sessions().find(s => s.id === selected);
    return ownerCard({ id: '', owner: selected, title: nameOf(s ?? { id: selected }), generation: '', reason: '', phase: 'queued' }, 'Requesting from');
  }
  function firstUse() {
    return h('div', { class: 'quiet-intro' }, helpReturn === 'state' && deps.state()?.active ? ownerCard(deps.state()!.active!, 'Owner') : selected ? requestingCard() : null, h('h2', {}, 'Need the machine for heavy work?'),
      h('p', { class: 'mut' }, 'Piper asks other sessions to checkpoint, stop their own jobs and wait. Once ready, run your work, then release the window and others continue.'),
      h('div', { class: 'quiet-steps-intro' }, ...[['Ask for quiet', 'Other sessions checkpoint, stop their own jobs, then wait.'], ['Work when ready', 'Run one task or several in a row. The window stays yours.'], ['Release and resume', 'Release when you are done, however long it takes. Others continue.']].map(([title, text], i) => h('div', {}, h('span', { class: 'quiet-number' }, String(i + 1)), h('strong', {}, title!), h('p', { class: 'mut' }, text!)))),
      h('div', { class: 'quiet-label' }, 'Good for'), h('div', { class: 'quiet-examples' }, ...['Full build', 'Large test run', 'Data import', 'Local model run'].map(t => h('span', { class: 'chip' }, t))), scope(),
      buttons(btn('Got it', () => { deps.markSeen(); mode = helpReturn === 'intro' ? 'request' : helpReturn; show(); }), btn(helpReturn === 'state' ? 'Back to quiet window' : 'Request a quiet window', () => { deps.markSeen(); mode = helpReturn === 'state' ? 'state' : 'request'; show(); }, true)));
  }
  function request() {
    const input = h('input', { id: 'quiet-reason', value: reason, maxlength: 300, required: true, placeholder: 'For example: full build of Atlas', oninput: (e: Event) => { reason = (e.target as HTMLInputElement).value; saveDraft(); submit.disabled = busy || !reason.trim() || !deps.state() || !!deps.outdated?.(); } });
    const submit = btn('Request quiet window', () => undefined, true); submit.type = 'submit'; submit.disabled = busy || !reason.trim() || !deps.state() || !!deps.outdated?.();
    const count = deps.sessions().filter(s => s.id !== selected && ['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting', 'ready', 'working', 'waiting', 'attention', 'stopping'].includes(s.state)).length;
    return h('form', { class: 'quiet-request', onsubmit: (e: Event) => { e.preventDefault(); if (!deps.state()) { deps.error('Quiet window is unavailable in this running controller. Restart Piper after updating it.'); return; } if (reason.trim() && !deps.outdated?.()) void action({ action: 'request', session: selected, reason }); } },
      requestingCard(), h('h2', {}, 'Request a quiet window'), h('p', { class: 'mut' }, 'Other Piper sessions checkpoint, stop their own jobs and wait until the window is released.'),
      h('label', { class: 'quiet-label', for: 'quiet-reason' }, 'What do you need quiet for?'), input,
      h('div', { class: 'quiet-label' }, 'What happens'), h('div', { class: 'quiet-request-points' },
        h('p', {}, `${count} other sessions will be asked to pause.`), h('p', {}, 'Work starts when every session confirms waiting and Piper sees no managed job running.'),
        h('p', {}, 'Until you release it: no time limit, so a 3 h job is fine. Your session checks in about every minute and normally releases the window itself when done. Release stays here as a fallback.')),
      scope(), buttons(btn('Cancel', () => { discardDraft(); deps.close(); }), submit));
  }
  function release(a: QuietRequest) {
    deps.confirm('Release quiet window', `Release ${nameOf(a)}’s window only after all its work has ended, including background tasks and subagents. Other sessions will be asked to continue.`, () => { void action({ action: 'release', id: a.id, finished: true }); });
  }
  function exemptButton(p: QuietParticipant, a: QuietRequest, title: string) {
    const allowed = p.exempt === 'allowed', owner = quietOwner(a, null, deps.sessions())?.name || nameOf(a);
    return h('button', { type: 'button', class: 'ghost', disabled: busy, 'data-quiet-focus': `${allowed ? 'remove' : 'grant'} ${p.id}`, 'aria-label': `${allowed ? 'Remove exception of' : 'Let continue'} ${title}`,
      onclick: () => allowed
        ? deps.confirm(`Remove ${title}’s exception?`, `${title} can’t start new work and must stop its own work and confirm again. The window stays shared until it does.`, () => { void action({ action: 'exempt', id: a.id, session: p.id, allow: false }); }, 'Remove exception')
        : deps.confirm(`Let ${title} keep working?`, `${title} continues without limits, including builds and tests. The window becomes shared, not quiet, so ${owner}’s results may be affected. The exception lasts only for this window and ends if ${title} restarts. You can remove it here at any time.`, () => { void action({ action: 'exempt', id: a.id, session: p.id, allow: true }); }, `Let ${title} continue`) },
      allowed ? 'Remove exception…' : 'Let continue…');
  }
  function rows(parts: QuietParticipant[], a: QuietRequest | null) {
    return h('div', { class: 'quiet-participants' }, h('div', { class: 'quiet-participant quiet-table-head', 'aria-hidden': 'true' }, ...['Session', 'Status', 'Detail', 'Source', ''].map(t => h('span', {}, t))), ...parts.map(p => {
      const v = quietParticipant(p, a); const title = nameOf(p);
      return h('div', { class: 'quiet-participant', 'data-quiet-participant': p.id }, h('strong', { class: 'quiet-participant-name', title }, title), h('span', { class: `quiet-participant-status ${v.tone}` }, v.label),
        h('span', { class: 'mut quiet-participant-detail', title: v.detail }, v.detail), h('span', { class: 'quiet-source' }, v.source),
        h('div', { class: 'quiet-row-actions' }, a && a.phase !== 'released' && p.status !== 'unknown' && deps.sessions().some(s => s.id === p.id) ? exemptButton(p, a, title) : null,
          deps.sessions().some(s => s.id === p.id) ? h('button', { type: 'button', class: 'ghost', 'aria-label': `Open session ${title}`, title, onclick: () => deps.open(p.id) }, 'Open') : null));
    }));
  }
  function activity(r: QuietRequest | null, a: QuietRequest | null) {
    const st = deps.state(), events = (st?.events ?? []).filter(e => e.window === r?.id), shown = allEvents ? events : events.slice(-8), dropped = r ? st?.event_dropped?.[r.id] ?? 0 : 0;
    return h('details', { class: 'quiet-details', open: details, ontoggle: (e: Event) => { details = (e.target as HTMLDetailsElement).open; } },
      h('summary', { 'data-quiet-focus': 'details' }, `Details · Activity · ${events.length} events`),
      h('div', { class: 'quiet-activity' }, h('div', { class: 'quiet-label' }, 'Activity'),
        !events.length ? h('p', { class: 'mut' }, 'No recorded events for this window. History is recorded from this update onward.') : null,
        events.length && !dropped && !events.some(e => e.kind === 'requested') ? h('p', { class: 'mut' }, 'Earlier activity for this window was not recorded. History starts with this update.') : null,
        ...shown.map(e => h('div', { class: 'quiet-event', 'data-event-seq': String(e.seq) }, h('time', { class: 'mono mut' }, quietTime(e.at)),
          deps.sessions().some(s => s.id === e.session && (!e.generation || e.generation.startsWith(`${s.created}:${s.pid}:`))) ? h('button', { class: 'ghost', onclick: () => deps.open(e.session), 'aria-label': `Open event session ${quietText(e.title, 'Session')}` }, quietText(e.title, 'Session')) : h('span', { class: 'mut', title: quietText(e.title) }, quietText(e.title, 'Piper')),
          h('div', {}, h('span', {}, EVENT_LABELS[e.kind] || 'Window updated'), quietText(e.detail) ? h('span', { class: 'mut' }, ' · ' + e.detail) : null), h('span', { class: 'quiet-source' }, quietText(e.source, 'Piper')))),
        events.length > 8 ? btn(allEvents ? 'Show recent events' : `Showing last ${shown.length} of ${events.length} · Show all`, () => { allEvents = !allEvents; show(); }) : null,
        dropped ? h('p', { class: 'mut' }, `${dropped} earlier events omitted.`) : null,
        h('details', { class: 'quiet-technical', open: technical, ontoggle: (e: Event) => { technical = (e.target as HTMLDetailsElement).open; } }, h('summary', { 'data-quiet-focus': 'technical' }, 'Technical details'), scope(),
          r ? h('dl', {}, h('dt', {}, 'Window'), h('dd', { class: 'mono' }, quietText(r.id, 'Unavailable')), h('dt', {}, 'Duration'), h('dd', {}, quietDuration(r.minutes))) : null,
          ...partsOf(a).filter(p => p.status === 'finishing' && (p.stale ?? (finite(p.at ?? p.delivered_at ?? a?.selected) && now() - (p.at ?? p.delivered_at ?? a?.selected)! >= 120)) && (['draining', 'settling'].includes(a!.phase) || p.exempt === 'returning')).map(p => h('div', { class: 'quiet-queued', 'data-quiet-manual': p.id }, h('span', {}, `${nameOf(p)} · no recent update`),
            btn('Confirm waiting…', () => deps.confirm(`Confirm ${nameOf(p)} is waiting?`, 'Confirm only after checking that all work in this session has ended, including background tasks and subagents. Piper cannot verify processes outside Piper.', () => { void action({ action: 'ack', id: a!.id, session: p.id, finished: true }); }, 'Confirm waiting')))),
          a && a.phase !== 'recovering' && finite(a.lease_until) && now() - (a.lease_until - 90) >= 60 ? h('div', { class: 'quiet-queued' }, h('span', {}, 'Manual fallback · only if the owner cannot check in'), btn('Still needed', () => { void action({ action: 'renew', id: a.id, session: a.owner }); })) : null,
          ...(st?.starts ?? []).map(q => h('div', { class: 'quiet-queued' }, h('span', {}, `${quietText(q.title, quietText(q.project, 'Session'))} · ${quietText(q.phase, 'Waiting')}${quietText(q.error) ? ': ' + q.error : ''}`),
            h('button', { type: 'button', disabled: busy || q.phase === 'checking' || q.phase === 'started', onclick: () => {
              if (q.phase === 'uncertain') deps.confirm('Remove this session record?', 'Check Overview first. This removes only the queued record, not a session that may already be running.', () => { void action({ action: 'cancel_start', id: q.id, checked: true }); }, 'I checked Overview, remove record');
              else void action({ action: 'cancel_start', id: q.id });
            } }, 'Remove queued start'))))));
  }
  function status() {
    const st = deps.state(), a = st?.active ?? null, last = st?.last ?? null, queue = st?.queue ?? [], mine = queue.find(q => q.owner === selected);
    if (!a && !last && !mine && !st?.starts?.length) { mode = deps.seen() ? 'request' : 'intro'; helpReturn = 'request'; return mode === 'request' ? request() : firstUse(); }
    if (mine && a) {
      const n = queue.indexOf(mine) + 1, owner = quietOwner(a, null, deps.sessions())!;
      return h('div', { class: 'quiet-state quiet-queued-state' }, h('div', { class: 'quiet-top' }, h('strong', { class: 'warn', role: 'status' }, `Waiting for ${owner.name}’s window to end · ${n === 1 ? 'you’re next' : `#${n} in queue`}`), btn('Cancel my request', () => { void action({ action: 'cancel', id: mine.id }); })),
        ownerCard(a, 'Current holder'), ownerCard(mine, 'Your request'), h('div', { class: 'quiet-who' }, ...[['Your window', 'not started yet'], ['Starts', `after ${owner.name} releases it`], ['In queue', `${queue.length} waiting · you’re #${n}`]].map(([label, value]) => h('div', {}, h('span', { class: 'quiet-label' }, label!), h('span', { title: value }, value!)))),
        h('p', { class: 'mut' }, 'When your turn comes, Piper asks the other sessions to wait. Your window starts once they confirm.'),
        h('div', { class: 'quiet-label' }, 'Queue'), h('div', { class: 'quiet-participants' },
          h('div', { class: 'quiet-participant quiet-table-head quiet-queue-row', 'aria-hidden': 'true' }, ...['Session', 'Position', 'Detail', 'Source'].map(t => h('span', {}, t))),
          ...[a, ...queue].map((q, i) => h('div', { class: 'quiet-participant quiet-queue-row' }, h('strong', { class: 'quiet-participant-name', title: nameOf(q) }, `${nameOf(q)}${q.id === mine.id ? ' (you)' : ''}`),
            h('span', { class: `quiet-participant-status ${i === 0 ? 'mut' : 'warn'}` }, i === 0 ? 'Holding' : i === 1 ? 'Next · #1' : `#${i}`),
            h('span', { class: 'mut quiet-participant-detail' }, `${quietText(q.reason, 'Heavy work')}${i && finite(q.requested) ? ` · requested ${quietTime(q.requested)}` : ''}`),
            h('span', { class: 'quiet-source' }, i === 0 ? 'Piper' : quietText(q.requested_by, 'Not recorded'))))), activity(mine, null));
    }
    const r = a ?? last, owner = quietOwner(a, last, deps.sessions()), model = quietStatus(a && owner ? { ...a, title: owner.name } : a), parts = partsOf(r);
    const operations = st?.operations ?? [], pending = parts.filter(p => p.resume?.status !== 'sent').length;
    const heading = h('div', { class: 'quiet-top' }, h('strong', { class: model.tone, role: 'status' }, !r ? 'Sessions waiting to start' : model.label),
      a ? (['draining', 'settling'].includes(a.phase) || a.phase === 'recovering' && !a.ready) ? btn('Cancel request', () => { void action({ action: 'cancel', id: a.id }); }) : btn(a.phase === 'recovering' ? 'Release after checking…' : 'Release window', () => release(a)) : btn('Request another window', () => { mode = 'request'; reason = ''; show(); }, true));
    const canWork = !a ? 'All sessions' : ['ready', 'working'].includes(a.phase) ? owner?.name || nameOf(a) : a.phase === 'recovering' ? 'Check needed' : 'Nobody yet';
    const allowed = quietExempt(a, 'allowed'), returning = quietExempt(a, 'returning'), held = parts.filter(p => !p.exempt);
    const facts = [['Can work', canWork]];
    if (allowed.length) facts.push(['Also working', `${names(allowed)} · exception`]);
    if (returning.length) facts.push(['Returning', `${names(returning)} · stopping`]);
    facts.push(['Others', !a ? `${parts.length - pending} notices delivered · ${pending} pending` : `${held.filter(p => p.status === 'waiting').length} of ${held.length} waiting`]);
    if (a) facts.push(['Piper jobs', operations.length ? `${operations.length} running` : 'none running']);
    const who = h('div', { class: 'quiet-who', 'aria-label': 'Who can work' }, ...facts.map(([label, value]) => h('div', {}, h('span', { class: 'quiet-label' }, label!), h('span', { title: value }, value!))));
    const explanation = !a ? `${parts.length - pending} of ${parts.length} continue notices delivered.${pending ? ` ${pending} still pending; Piper keeps retrying.` : ''}` : a.phase === 'recovering' ? quietText(a.problem, 'Supervision was lost. Check that the work has ended before releasing.')
      : ['ready', 'working'].includes(a.phase) ? 'Your session reports its work here and normally releases the window itself when done. Release is the fallback. Others keep waiting until the window is released.'
      : operations.length ? 'Waiting for Piper jobs to end before the window can start.' : 'Sessions checkpoint and stop their own jobs, then confirm. Your work can’t start yet.';
    return h('div', { class: 'quiet-state' }, heading, r ? ownerCard(r, a ? 'Owner' : 'Last owner', !!a && ['ready', 'working', 'recovering'].includes(a.phase)) : null, who,
      h('p', { class: 'mut quiet-explanation' }, explanation),
      allowed.length ? h('div', { class: 'quiet-stale ibx-secnote warn', role: 'note' }, h('strong', {}, `Not quiet: ${names(allowed)} ${allowed.length === 1 ? 'has an exception' : 'have exceptions'}`),
        h('p', {}, `You let ${names(allowed)} keep working, so this window no longer guarantees quiet and ${owner?.name || nameOf(a)}’s results may be affected. Remove the exception to make ${allowed.length === 1 ? 'it' : 'them'} stop and confirm again.`)) : null,
      a?.phase === 'recovering' ? h('p', { class: 'quiet-stale ibx-secnote warn' }, a.ready ? 'Open the owner and check that all work has ended before releasing. Nothing resumes on its own.' : 'No work was allowed. Cancel this request to let the sessions continue.') : null,
      operations.length ? h('div', { class: 'quiet-jobs' }, ...operations.map(x => h('div', { class: 'quiet-queued' }, h('span', {}, `Piper job: ${quietText(x.kind, 'Managed work')}`), deps.sessions().some(s => s.id === x.session) ? btn(`Open ${nameOf(deps.sessions().find(s => s.id === x.session))}`, () => deps.open(x.session)) : h('span', { class: 'mut' }, 'Piper')))) : null,
      h('div', { class: 'quiet-label' }, 'Other sessions'), rows(parts, a), scope(), activity(r, a),
      queue.length ? h('div', { class: 'quiet-queue-summary' }, h('div', { class: 'quiet-label' }, 'Next requests'), ...queue.map((q, i) => h('div', { class: 'quiet-queued' }, h('span', {}, `${i + 1}. ${nameOf(q)} · ${quietText(q.reason, 'Heavy work')}`), btn('View request', () => { selected = q.owner; show(); })))) : null);
  }
  function update() {
    const st = deps.state();
    // Heartbeats and clock ticks update only their text, not the interactive DOM.
    const sig = JSON.stringify([mode, selected, mode === 'state' ? st : st?.enabled, deps.outdated?.(), busy, allEvents, st?.active?.phase !== 'recovering' && finite(st?.active?.lease_until) && now() - (st.active.lease_until - 90) >= 60,
      deps.sessions().map(s => [s.id, s.title, s.agent, ['exited', 'error'].includes(s.state)])], (key, value) => ['now', 'lease_until'].includes(key) ? undefined : value);
    if (sig === signature) {
      for (const node of el.querySelectorAll?.<HTMLElement>('[data-quiet-check]') ?? []) { const a = st?.active; const at = finite(a?.lease_until) ? a.lease_until - 90 : Number(node.dataset.quietCheck); node.textContent = markBidi(` · last check-in ${ago(at, now())}`); }
      for (const row of el.querySelectorAll?.<HTMLElement>('[data-quiet-participant]') ?? []) {
        const p = partsOf(st?.active).find(p => p.id === row.dataset.quietParticipant);
        if (p) { const detail = row.querySelector<HTMLElement>('.quiet-participant-detail'); if (detail) { const v = quietParticipant(p, st!.active!); detail.textContent = markBidi(v.detail); detail.title = markBidi(v.detail); } }
      }
      return;
    }
    signature = sig;
    const scroll = el.scrollTop, focused = typeof document.activeElement !== 'undefined' && el.contains?.(document.activeElement) ? document.activeElement as HTMLElement : null;
    const focusKey = focused?.id || focused?.getAttribute('aria-label') || focused?.getAttribute('data-quiet-focus');
    clear(el);
    if (deps.outdated?.()) el.append(h('div', { class: 'quiet-update ibx-secnote', role: 'status' }, h('span', {}, 'Piper was updated. Reload to use the latest Quiet window. Your draft stays.'), btn('Reload', () => { saveDraft(); deps.reload?.(); })));
    el.append(mode === 'intro' ? firstUse() : mode === 'request' ? request() : status());
    el.scrollTop = scroll;
    if (focusKey) Array.from(el.querySelectorAll?.<HTMLElement>('button,input,summary') ?? []).find(n => n.id === focusKey || n.getAttribute('aria-label') === focusKey || n.getAttribute('data-quiet-focus') === focusKey)?.focus({ preventScroll: true });
  }
  return { el, update, restoreDraft() {
      try { const raw = sessionStorage.getItem('piper.quiet.draft'); sessionStorage.removeItem('piper.quiet.draft'); const d = raw ? JSON.parse(raw) : null; if (d && typeof d.selected === 'string' && typeof d.reason === 'string' && d.mode === 'request' && deps.sessions().some(s => s.id === d.selected)) { selected = d.selected; reason = d.reason.slice(0, 300); mode = 'request'; returnable = true; show(); return true; } } catch { /* unavailable or malformed draft */ } return false;
    }, header: () => h('header', { class: 'head quiet-head' }, fi('settings'), h('strong', {}, 'Quiet window'), h('span', { class: 'mut' }, 'This computer'), h('span', { class: 'grow' }),
      h('button', { class: 'ghost', onclick: () => { if (mode === 'intro') mode = helpReturn; else { helpReturn = mode; mode = 'intro'; } show(); } }, fi('info'), 'How it works')),
    returnable: (sid: string) => returnable && sid === selected,
    open(sid: string, fresh = false) { const previous = selected; returnable = true; const changed = selected !== sid; selected = sid; if (fresh) reason = ''; if (changed) allEvents = false; const existing = deps.state()?.active?.owner === sid || deps.state()?.queue?.some(q => q.owner === sid); if (!fresh && previous === sid && (mode === 'request' || mode === 'intro') && !existing) { show(); return; } mode = existing || !fresh && (deps.state()?.active || deps.state()?.starts?.length || deps.state()?.last) ? 'state' : deps.seen() ? 'request' : 'intro'; helpReturn = mode === 'intro' ? 'request' : mode; show(); },
    banner(open: (sid?: string) => void, draft = false) {
      const a = deps.state()?.active, pending = deps.state()?.starts?.length ?? 0;
      if (!a && !draft && !pending) return null;
      const owner = quietOwner(a, mode === 'state' ? deps.state()?.last : null, deps.sessions()), queue = deps.state()?.queue ?? [], mine = a && queue.find(q => q.owner === selected);
      return h('div', { class: 'quiet-banner-wrap' }, h('button', { class: 'quiet-banner', onclick: () => open(mine ? mine.owner : a?.owner) }, fi('settings'), h('span', {}, h('strong', {}, 'Quiet window'),
        h('small', { class: mine ? 'warn' : a ? quietStatus(a).tone : 'mut' }, mine ? `Queued · ${queue.indexOf(mine) === 0 ? 'you’re next' : `#${queue.indexOf(mine) + 1}`}` : a ? quietStatus({ ...a, title: owner?.name || nameOf(a) }).label : pending ? `${pending} starts queued` : owner ? 'Window released' : 'Setting up a request')), h('span', {}, '›')),
        owner ? h('button', { class: 'quiet-banner-owner', disabled: !owner.canOpen, title: owner.name, 'aria-label': `Open owner session ${owner.name}`, onclick: () => deps.open(owner.id) }, `${mine ? 'Holder' : owner.role}: ${owner.name} ↗`) : null);
    } };
}
