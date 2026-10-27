// The one-time Security screen after the Minimal change: why Minimal grew,
// what it now switches on, and the profile to keep. /api/state `security_notice` carries it until it was
// shown once (POST /api/features {seen}); a saved own mix is only informed, nothing is preselected.

import { ApiError, call } from './api.js';
import { h } from './dom.js';
import { markBidi } from './text.js';
import type { SecurityNotice } from './types.js';

export const NOTICE_ID = 'security-2';
const NOTICE_TITLE = 'Minimal now protects your machine too';

/** what Minimal added on 2026-10-16, in the user's words (security.DEFAULT_ON minus LEGACY_MINIMAL) */
export const NOTICE_ITEMS = [
  "Piper's own directories are never mounted into a sandbox",
  "No sandbox reaches Piper's port or the host Docker API",
  'Host git ignores hooks and config a sandbox planted',
  "Editing another project's hooks, CI or build files asks first",
  "A session reaches only its own project's dev stack",
  'A project cannot bring its own agent image',
];

/** [preset id, label, effect] - the profiles the screen offers, in the Settings wording */
export const NOTICE_CHOICES: [string, string, string][] = [
  ['hole', 'Minimal', 'Production safeguards, host protections and a few approvals. Nothing blocks a start.'],
  ['recommended', 'Recommended', 'Also masks production data and asks before push, merge and cross-project writes.'],
  ['guarded', 'Strict', 'All guards and approvals. Expect more checks and interruptions.'],
];

/** the profile preselected: the current one, Minimal for an own mix (never offered there) */
export function noticeSelected(n: SecurityNotice): string {
  return NOTICE_CHOICES.some((c) => c[0] === n.profile) ? n.profile : 'hole';
}

/** the primary button: keeping the current profile only closes, another one applies */
export function noticeAction(n: SecurityNotice, chosen: string): { label: string; apply: boolean } {
  const label = NOTICE_CHOICES.find((c) => c[0] === chosen)?.[1] ?? 'Minimal';
  return chosen === n.profile ? { label: `Keep ${label}`, apply: false } : { label: `Use ${label}`, apply: true };
}

/** whether to open it now: once per page, never over the setup or another dialog */
export function noticeWanted(n: SecurityNotice | null | undefined, shown: boolean, busy: boolean): boolean {
  return !!n && !shown && !busy;
}

export interface NoticeDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  toast(text: string): void;
  openSecurity(): void;
}

const CHECK = 'M20 6 9 17l-5-5';
const INFO = ['M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20z', 'M12 16v-4', 'M12 8h.01'];
const SHIELD = ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z', 'M9 12h6', 'M12 9v6'];

function icon(paths: string[], cls: string): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', cls);
  for (const d of paths) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/** marks the screen seen at once (one time, also when closed with Esc or in another tab) and opens it */
export function showSecurityNotice(n: SecurityNotice, deps: NoticeDeps): void {
  void call('POST', '/api/features', { seen: NOTICE_ID }).catch(() => { /* shown again on the next load */ });
  const custom = n.kind !== 'profile';
  let chosen = noticeSelected(n);
  const primary = h('button', { class: 'primary', 'data-notice': 'apply' });
  const sync = () => { primary.textContent = markBidi(custom ? 'Open Security settings' : noticeAction(n, chosen).label); };
  const choices = custom ? null : h('fieldset', { class: 'sn-choices' }, h('legend', {}, 'Your protection level'),
    ...NOTICE_CHOICES.map(([id, label, effect]) => {
      const input = h('input', { type: 'radio', name: 'sn-profile', value: id, checked: id === chosen,
        onchange: () => { chosen = id; for (const l of choices!.querySelectorAll('label')) l.classList.toggle('on', l.contains(input)); sync(); } });
      return h('label', { class: 'sn-choice' + (id === chosen ? ' on' : '') }, input,
        h('span', { class: 'sn-choice-body' },
          h('span', { class: 'sn-choice-title' }, h('b', {}, label), id === n.profile ? h('span', { class: 'sn-current' }, 'CURRENT') : null),
          h('span', { class: 'mut small' }, effect)));
    }));
  primary.addEventListener('click', () => {
    if (custom) { deps.closeModal(); deps.openSecurity(); return; }
    const act = noticeAction(n, chosen);
    if (!act.apply) { deps.closeModal(); return; }
    primary.setAttribute('disabled', '');
    void applyProfile(chosen).then((ok) => {
      deps.closeModal();
      if (ok) { deps.toast(`Security: ${act.label.slice(4)} applied.`); return; }
      deps.toast(`${act.label.slice(4)} weakens the protection: choose it in Settings › Security, which asks you to confirm.`);
      deps.openSecurity();
    }, (e) => { primary.removeAttribute('disabled'); deps.toast('Not saved: ' + String(e instanceof Error ? e.message : e)); });
  });
  sync();
  const second = custom
    ? h('button', { 'data-notice': 'dismiss', onclick: deps.closeModal }, 'Got it')
    : h('button', { 'data-notice': 'settings', onclick: () => { deps.closeModal(); deps.openSecurity(); } }, 'Customize in Settings…');
  deps.modal(NOTICE_TITLE,
    h('div', { class: 'sn' },
      h('div', { class: 'sn-head' }, h('span', { class: 'sn-tile' }, icon(SHIELD, 'sn-ic')),
        h('span', { class: 'sn-head-text' }, h('b', { class: 'sn-title' }, NOTICE_TITLE),
          h('span', { class: 'sn-kicker' }, 'Security · after the update · shown once'))),
      h('p', { class: 'mut' }, 'Piper used to add many checks on top of hole and asked too often, so every guard became a switch that starts off, like a plain hole. ' +
        'A few protections close real paths from a sandbox to your machine and never get in the way. Minimal now switches them on.'),
      h('div', { class: 'sn-new' }, h('div', { class: 'sn-label' }, 'NEW IN MINIMAL'),
        h('ul', {}, ...NOTICE_ITEMS.map((t) => h('li', {}, icon([CHECK], 'sn-ok'), t))),
        custom ? null : h('p', { class: 'mut small' }, 'Still in Minimal: production database and HTTP ask first, production writes need WHERE, Piper settings and new domains ask.')),
      custom
        ? h('div', { class: 'sn-own' }, icon(INFO, 'sn-info'), h('span', { class: 'sn-choice-body' },
          h('b', {}, `Your settings stay as they are · Custom, ${n.on} of ${n.total}`),
          h('span', { class: 'mut small' }, 'You chose your own guards, so nothing changed. Switch on the new items yourself, or pick Minimal, in Settings › Security.')))
        : choices,
      custom ? null : h('p', { class: 'mut small' }, 'Tool rules apply at once, sandbox protections from the next session start. You can change this any time in Settings › Security.'),
      h('div', { class: 'row sn-actions' }, second, h('span', { class: 'grow' }), primary)));
}

/** a profile through Settings (the same validation and audit as its Apply); false = it weakens the isolation,
 *  which only Settings confirms (its "Weakening the isolation" dialog) */
async function applyProfile(id: string): Promise<boolean> {
  const st = await call<{ sections: { id: string; rev?: string; fields: { key: string; presets?: Record<string, Record<string, boolean>> }[] }[] }>('GET', '/api/settings');
  const sec = st.data?.sections.find((s) => s.id === 'security');
  const want = sec?.fields.find((f) => f.key === 'security')?.presets?.[id];
  if (!sec || !want) throw new Error('this profile needs the updated Piper server');
  try {
    await call('POST', '/api/settings', { section: 'security', values: { security: want }, security_profile: id, rev: sec.rev });
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && Array.isArray((e.json as { needs_confirm?: unknown } | null)?.needs_confirm)) return false;
    throw e;
  }
  return true;
}
