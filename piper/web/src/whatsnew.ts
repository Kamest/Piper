// The one-time "What's new" screen: what arrived since 2026-10-13, for a Piper home that was in use before the
// first open-source release. /api/state `whats_new` carries it until it was shown once (POST /api/features
// {seen}); piperd never offers it to a new home or next to the Security screen / the restore card, and the GUI
// waits for its next open when another one-time screen opened on this page load (main.ts syncFeatures).

import { call } from './api.js';
import { h } from './dom.js';
import type { WhatsNewNotice } from './types.js';

export const NOTICE_ID = 'whats-new-1';
export const CHANGELOG_URL = 'https://github.com/Kamest/Piper/blob/main/CHANGELOG.md';
/** the project page's downloads (macOS, Windows, Linux) - it names the current desktop release */
export const DOWNLOAD_URL = 'https://kamest.github.io/Piper/#desktop';
/** the open-source repository */
export const REPO_URL = 'https://github.com/Kamest/Piper';
/** the phone client */
export const PHONE_URL = 'https://piper.kamest.dev';

export interface WhatsNewItem { icon: string[]; title: string; text: string; tag?: string; link?: { label: string; href: string } }

// Lucide line icons (ISC), 24 x 24, stroke 2
const I = {
  monitor: ['M4 3h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z', 'M8 21h8', 'M12 17v4'],
  phone: ['M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z', 'M12 18h.01'],
  server: ['M4 3h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z',
    'M4 13h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2z', 'M6 7h.01', 'M6 17h.01'],
  check: ['M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z',
    'M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2', 'm9 14 2 2 4-4'],
  compare: ['M15 18a3 3 0 1 0 6 0a3 3 0 1 0-6 0', 'M3 6a3 3 0 1 0 6 0a3 3 0 1 0-6 0', 'M13 6h3a2 2 0 0 1 2 2v7', 'M11 18H8a2 2 0 0 1-2-2V9'],
  swap: ['M8 3 4 7l4 4', 'M4 7h16', 'm16 21 4-4-4-4', 'M20 17H4'],
  history: ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5', 'M12 7v5l4 2'],
  sparkles: ['M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z',
    'M20 3v4', 'M22 5h-4', 'M4 17v2', 'M5 18H3'],
  external: ['M7 7h10v10', 'M7 17 17 7'],
  download: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5', 'M12 15V3'],
  github: ['M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4',
    'M9 18c-4.51 2-5-2-7-2'],
};

/** Cumulative migration highlights since 2026-10-13 (CHANGELOG 0.1.0-0.1.8, desktop 0.1.2-0.1.15). */
export const WHATS_NEW_ITEMS: WhatsNewItem[] = [
  { icon: I.monitor, title: 'Desktop app', text: 'A window and tray for macOS, Windows and Linux. Answer cards from a notification, put sessions in more windows.',
    link: { label: 'Download', href: DOWNLOAD_URL } },
  { icon: I.phone, title: 'Piper on your phone', tag: 'BETA', text: 'Pair in Settings › Mobile access. Answer cards and follow sessions, no VPN.',
    link: { label: 'Open piper.kamest.dev', href: PHONE_URL } },
  { icon: I.server, title: 'Your own Piper server', tag: 'EXPERIMENTAL', text: 'Settings › Servers sets up a Linux server over SSH, step by step. Move to server continues a session there.' },
  { icon: I.monitor, title: 'Workspaces', text: 'In the desktop app, switch workspaces from the window bar without reloading open terminals. If one window has to recover, the rest of the app stays online.' },
  { icon: I.compare, title: 'Review after each turn', text: 'The Review tab, line notes for the agent and Revert to here with an Undo backup. Quicker on large repositories.' },
  { icon: I.check, title: 'Cards show the facts', text: 'A push card lists commits, diffstat and a secret scan. Database writes get a dry run first.' },
  { icon: I.swap, title: 'Continue with the other agent', text: 'Hand a session from Claude to Codex or back. The conversation goes with it.' },
  { icon: I.history, title: 'Restarts keep your work', text: 'One piper update restarts Piper, and running sessions usually survive. Sessions it stops are offered for restore.' },
];

/** Smaller changes keep the main highlights readable without dropping the earlier migration news. */
export const WHATS_NEW_MORE: Pick<WhatsNewItem, 'title' | 'text'>[] = [
  { title: 'A calmer look', text: 'Sessions in the rail, one session bar, line icons, a Light, Dark or Indigo theme and your own font sizes.' },
  { title: 'New session', text: 'Clone a repository or create a project from New session. Your task draft and pasted images stay with it.' },
  { title: 'Phone backup', text: 'Your phone can also pair one backup computer or server. Part of the phone beta.' },
  { title: 'Later', text: 'Set any session aside for Later. An ended one opens its Conversation without restarting the agent.' },
  { title: 'Wrapped links', text: "A path or URL the terminal wrapped stays one link. A path into another project opens that project's Files." },
  { title: 'Stack overview', text: 'Optional feature: every running dev stack in one place. Stop all lists its targets first and keeps data.' },
  { title: 'Git renames', text: 'Git windows show a moved file with its original path and stage both sides together.' },
  { title: 'Extensions', text: 'Load extensions from Settings.' },
];

/** whether to open it now: once per page, never over a dialog, never on a page load that already showed another
 *  one-time screen (setup, Security, restore) - then it waits for the next open of the GUI */
export function whatsNewWanted(n: WhatsNewNotice | null | undefined, shown: boolean, blockedThisPage: boolean, busy: boolean): boolean {
  return !!n && !shown && !blockedThisPage && !busy;
}

export interface WhatsNewDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
}

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag: string, attrs: Record<string, string | number>): SVGElement {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function icon(paths: string[], cls: string): SVGSVGElement {
  const svg = svgEl('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true', class: cls }) as SVGSVGElement;
  for (const d of paths) svg.append(svgEl('path', { d }));
  return svg;
}

/** the hero illustration: the desktop window with an approval card, the phone beside it (theme tokens via classes) */
export function illustration(): SVGSVGElement {
  const svg = svgEl('svg', { viewBox: '0 0 280 160', 'aria-hidden': 'true', class: 'wn-art' }) as SVGSVGElement;
  const r = (x: number, y: number, w: number, hh: number, rx: number, cls: string) =>
    svgEl('rect', { x, y, width: w, height: hh, rx, class: cls });
  const clip = svgEl('clipPath', { id: 'wn-win' });
  clip.append(svgEl('rect', { x: 8, y: 14, width: 196, height: 128, rx: 10 }));
  const win = svgEl('g', { 'clip-path': 'url(#wn-win)' });
  win.append(r(8, 14, 196, 128, 0, 'wn-a-tile'), r(8, 38, 40, 104, 0, 'wn-a-well'), svgEl('path', { d: 'M8 37.75H204', class: 'wn-a-rule' }),
    svgEl('circle', { cx: 22, cy: 26, r: 3, class: 'wn-a-faint' }), svgEl('circle', { cx: 32, cy: 26, r: 3, class: 'wn-a-faint' }),
    svgEl('circle', { cx: 42, cy: 26, r: 3, class: 'wn-a-faint' }),
    r(18, 50, 20, 8, 3, 'wn-a-link'), r(18, 66, 20, 8, 3, 'wn-a-line'), r(18, 82, 20, 8, 3, 'wn-a-line'),
    r(62, 52, 92, 6, 3, 'wn-a-line'), r(62, 66, 124, 6, 3, 'wn-a-line'), r(62, 80, 70, 6, 3, 'wn-a-line'));
  svg.append(svgEl('defs', {}), win, r(8, 14, 196, 128, 10, 'wn-a-frame'),
    r(92, 98, 128, 44, 8, 'wn-a-card'), svgEl('circle', { cx: 107, cy: 114, r: 4, class: 'wn-a-link' }),
    r(117, 109, 78, 6, 3, 'wn-a-mut'), r(134, 124, 36, 11, 4, 'wn-a-btn'), r(176, 124, 36, 11, 4, 'wn-a-acc'),
    r(214, 38, 58, 110, 12, 'wn-a-phone'), r(235, 45, 16, 4, 2, 'wn-a-line'),
    r(222, 58, 42, 16, 4, 'wn-a-sel'), svgEl('circle', { cx: 229, cy: 66, r: 2.5, class: 'wn-a-link' }),
    r(222, 80, 42, 16, 4, 'wn-a-tile'), r(222, 102, 42, 16, 4, 'wn-a-tile'), r(222, 124, 42, 14, 4, 'wn-a-acc'),
    svgEl('path', { d: 'M232 6L234.5 14.5L243 17L234.5 19.5L232 28L229.5 19.5L221 17L229.5 14.5Z', class: 'wn-a-link' }),
    svgEl('path', { d: 'M259 24L260.2 27.8L264 29L260.2 30.2L259 34L257.8 30.2L254 29L257.8 27.8Z', class: 'wn-a-link wn-a-dim' }));
  svg.querySelector('defs')!.append(clip);
  return svg;
}

/** marks the screen seen at once (one time, also when closed with Esc or in another window) and opens it */
export function showWhatsNew(deps: WhatsNewDeps): void {
  void call('POST', '/api/features', { seen: NOTICE_ID }).catch(() => { /* shown again on the next load */ });
  const cards = WHATS_NEW_ITEMS.map((it) => h('div', { class: 'wn-card' },
    h('span', { class: 'wn-tile' }, icon(it.icon, 'wn-ic')),
    h('span', { class: 'wn-card-body' },
      h('span', { class: 'wn-card-title' }, h('b', {}, it.title), it.tag ? h('span', { class: 'wn-tag' }, it.tag) : null),
      h('span', { class: 'wn-card-text' }, it.text),
      it.link ? h('a', { class: 'wn-card-link', href: it.link.href, target: '_blank', rel: 'noopener noreferrer' },
        it.link.label, icon(I.external, 'wn-link-ic')) : null)));
  deps.modal("What's new",
    h('div', { class: 'wn' },
      h('div', { class: 'wn-hero' },
        h('div', { class: 'wn-hero-text' },
          h('span', { class: 'wn-kicker' }, icon(I.sparkles, 'wn-kicker-ic'), "WHAT'S NEW"),
          h('b', { class: 'wn-title' }, 'Piper is open source now'),
          h('p', { class: 'wn-lead' }, 'Piper now comes from its open-source project. Your sessions, settings and logins are where you left them. ' +
            'Here is what arrived since mid-October.'),
          h('div', { class: 'wn-cta' },
            h('a', { class: 'wn-btn wn-btn-acc', href: DOWNLOAD_URL, target: '_blank', rel: 'noopener noreferrer' },
              icon(I.download, 'wn-link-ic'), 'Download the desktop app'),
            h('a', { class: 'wn-btn', href: REPO_URL, target: '_blank', rel: 'noopener noreferrer' },
              icon(I.github, 'wn-link-ic'), 'Piper on GitHub'))),
        illustration()),
      h('div', { class: 'wn-body', tabindex: '0', role: 'region', 'aria-label': 'New since your last update' },
        h('div', { class: 'wn-label' }, 'NEW SINCE YOUR LAST UPDATE'),
        h('div', { class: 'wn-grid' }, ...cards),
        h('div', { class: 'wn-label wn-more-label' }, 'ALSO NEW'),
        h('dl', { class: 'wn-more' }, ...WHATS_NEW_MORE.map((it) => h('div', { class: 'wn-more-row' },
          h('dt', {}, it.title), h('dd', {}, it.text))))),
      h('div', { class: 'wn-foot' },
        h('a', { class: 'wn-link', href: CHANGELOG_URL, target: '_blank', rel: 'noopener noreferrer' }, 'Full changelog', icon(I.external, 'wn-link-ic')),
        h('span', { class: 'wn-once' }, 'Shown once'),
        h('span', { class: 'grow' }),
        h('button', { class: 'primary', 'data-wn': 'close', onclick: deps.closeModal }, 'Got it'))));
}
