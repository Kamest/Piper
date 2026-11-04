// The one-time "What's new" screen: what arrived since 2026-10-13, for a Piper home that was in use before the
// first open-source release. /api/state `whats_new` carries it until it was shown once (POST /api/features
// {seen}); piperd never offers it to a new home or next to the Security screen / the restore card, and the GUI
// waits for its next open when another one-time screen opened on this page load (main.ts syncFeatures).
import { call } from './api.js';
import { h } from './dom.js';
export const NOTICE_ID = 'whats-new-1';
export const CHANGELOG_URL = 'https://github.com/Kamest/Piper/blob/main/CHANGELOG.md';
/** the project page's downloads (macOS, Windows, Linux) - it names the current desktop release */
export const DOWNLOAD_URL = 'https://kamest.github.io/Piper/#desktop';
/** the open-source repository */
export const REPO_URL = 'https://github.com/Kamest/Piper';
/** the phone client */
export const PHONE_URL = 'https://piper.kamest.dev';
// Lucide line icons (ISC), 24 x 24, stroke 2
const I = {
    monitor: ['M4 3h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z', 'M8 21h8', 'M12 17v4'],
    phone: ['M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z', 'M12 18h.01'],
    palette: ['M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z',
        'M13.5 6.5h.01', 'M17.5 10.5h.01', 'M8.5 7.5h.01', 'M6.5 12.5h.01'],
    check: ['M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z',
        'M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2', 'm9 14 2 2 4-4'],
    compare: ['M15 18a3 3 0 1 0 6 0a3 3 0 1 0-6 0', 'M3 6a3 3 0 1 0 6 0a3 3 0 1 0-6 0', 'M13 6h3a2 2 0 0 1 2 2v7', 'M11 18H8a2 2 0 0 1-2-2V9'],
    swap: ['M8 3 4 7l4 4', 'M4 7h16', 'm16 21 4-4-4-4', 'M20 17H4'],
    history: ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5', 'M12 7v5l4 2'],
    refresh: ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
    sparkles: ['M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z',
        'M20 3v4', 'M22 5h-4', 'M4 17v2', 'M5 18H3'],
    external: ['M7 7h10v10', 'M7 17 17 7'],
    download: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5', 'M12 15V3'],
    github: ['M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4',
        'M9 18c-4.51 2-5-2-7-2'],
};
/** what arrived since 2026-10-13, in the user's words (CHANGELOG 0.1.0-0.1.3, desktop 0.1.2-0.1.6) */
export const WHATS_NEW_ITEMS = [
    { icon: I.monitor, title: 'Desktop app', text: 'A window and tray for macOS, Windows and Linux. Answer cards from a notification, put sessions in more windows.',
        link: { label: 'Download', href: DOWNLOAD_URL } },
    { icon: I.phone, title: 'Piper on your phone', tag: 'BETA', text: 'Pair in Settings › Mobile access, then open piper.kamest.dev. Answer cards and follow sessions, no VPN.',
        link: { label: 'Open piper.kamest.dev', href: PHONE_URL } },
    { icon: I.palette, title: 'A calmer look', text: 'Your sessions in the rail, one session bar, line icons and a Light, Dark or Indigo theme.' },
    { icon: I.check, title: 'Cards show the facts', text: 'A push card lists commits, diffstat and a secret scan. Database writes get a dry run first.' },
    { icon: I.compare, title: 'Review after each turn', text: 'The Review tab, line notes for the agent and Revert to here with an Undo backup.' },
    { icon: I.swap, title: 'Continue with the other agent', text: 'Hand a session from Claude to Codex or back. The conversation goes with it.' },
    { icon: I.history, title: 'Nothing lost on restart', text: 'Sessions a restart stopped are offered for restore. Set any session aside for Later.' },
    { icon: I.refresh, title: 'Updates in one step', text: 'piper update pulls and restarts while sessions keep running. Load extensions from Settings.' },
];
/** whether to open it now: once per page, never over a dialog, never on a page load that already showed another
 *  one-time screen (setup, Security, restore) - then it waits for the next open of the GUI */
export function whatsNewWanted(n, shown, blockedThisPage, busy) {
    return !!n && !shown && !blockedThisPage && !busy;
}
const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs))
        el.setAttribute(k, String(v));
    return el;
}
function icon(paths, cls) {
    const svg = svgEl('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true', class: cls });
    for (const d of paths)
        svg.append(svgEl('path', { d }));
    return svg;
}
/** the hero illustration: the desktop window with an approval card, the phone beside it (theme tokens via classes) */
export function illustration() {
    const svg = svgEl('svg', { viewBox: '0 0 280 160', 'aria-hidden': 'true', class: 'wn-art' });
    const r = (x, y, w, hh, rx, cls) => svgEl('rect', { x, y, width: w, height: hh, rx, class: cls });
    const clip = svgEl('clipPath', { id: 'wn-win' });
    clip.append(svgEl('rect', { x: 8, y: 14, width: 196, height: 128, rx: 10 }));
    const win = svgEl('g', { 'clip-path': 'url(#wn-win)' });
    win.append(r(8, 14, 196, 128, 0, 'wn-a-tile'), r(8, 38, 40, 104, 0, 'wn-a-well'), svgEl('path', { d: 'M8 37.75H204', class: 'wn-a-rule' }), svgEl('circle', { cx: 22, cy: 26, r: 3, class: 'wn-a-faint' }), svgEl('circle', { cx: 32, cy: 26, r: 3, class: 'wn-a-faint' }), svgEl('circle', { cx: 42, cy: 26, r: 3, class: 'wn-a-faint' }), r(18, 50, 20, 8, 3, 'wn-a-link'), r(18, 66, 20, 8, 3, 'wn-a-line'), r(18, 82, 20, 8, 3, 'wn-a-line'), r(62, 52, 92, 6, 3, 'wn-a-line'), r(62, 66, 124, 6, 3, 'wn-a-line'), r(62, 80, 70, 6, 3, 'wn-a-line'));
    svg.append(svgEl('defs', {}), win, r(8, 14, 196, 128, 10, 'wn-a-frame'), r(92, 98, 128, 44, 8, 'wn-a-card'), svgEl('circle', { cx: 107, cy: 114, r: 4, class: 'wn-a-link' }), r(117, 109, 78, 6, 3, 'wn-a-mut'), r(134, 124, 36, 11, 4, 'wn-a-btn'), r(176, 124, 36, 11, 4, 'wn-a-acc'), r(214, 38, 58, 110, 12, 'wn-a-phone'), r(235, 45, 16, 4, 2, 'wn-a-line'), r(222, 58, 42, 16, 4, 'wn-a-sel'), svgEl('circle', { cx: 229, cy: 66, r: 2.5, class: 'wn-a-link' }), r(222, 80, 42, 16, 4, 'wn-a-tile'), r(222, 102, 42, 16, 4, 'wn-a-tile'), r(222, 124, 42, 14, 4, 'wn-a-acc'), svgEl('path', { d: 'M232 6L234.5 14.5L243 17L234.5 19.5L232 28L229.5 19.5L221 17L229.5 14.5Z', class: 'wn-a-link' }), svgEl('path', { d: 'M259 24L260.2 27.8L264 29L260.2 30.2L259 34L257.8 30.2L254 29L257.8 27.8Z', class: 'wn-a-link wn-a-dim' }));
    svg.querySelector('defs').append(clip);
    return svg;
}
/** marks the screen seen at once (one time, also when closed with Esc or in another window) and opens it */
export function showWhatsNew(deps) {
    void call('POST', '/api/features', { seen: NOTICE_ID }).catch(() => { });
    const cards = WHATS_NEW_ITEMS.map((it) => h('div', { class: 'wn-card' }, h('span', { class: 'wn-tile' }, icon(it.icon, 'wn-ic')), h('span', { class: 'wn-card-body' }, h('span', { class: 'wn-card-title' }, h('b', {}, it.title), it.tag ? h('span', { class: 'wn-tag' }, it.tag) : null), h('span', { class: 'wn-card-text' }, it.text), it.link ? h('a', { class: 'wn-card-link', href: it.link.href, target: '_blank', rel: 'noopener noreferrer' }, it.link.label, icon(I.external, 'wn-link-ic')) : null)));
    deps.modal("What's new", h('div', { class: 'wn' }, h('div', { class: 'wn-hero' }, h('div', { class: 'wn-hero-text' }, h('span', { class: 'wn-kicker' }, icon(I.sparkles, 'wn-kicker-ic'), "WHAT'S NEW"), h('b', { class: 'wn-title' }, 'Piper is open source now'), h('p', { class: 'wn-lead' }, 'Piper now comes from its open-source project. Your sessions, settings and logins are where you left them. ' +
        'Here is what is new.'), h('div', { class: 'wn-cta' }, h('a', { class: 'wn-btn wn-btn-acc', href: DOWNLOAD_URL, target: '_blank', rel: 'noopener noreferrer' }, icon(I.download, 'wn-link-ic'), 'Download the desktop app'), h('a', { class: 'wn-btn', href: REPO_URL, target: '_blank', rel: 'noopener noreferrer' }, icon(I.github, 'wn-link-ic'), 'Piper on GitHub'))), illustration()), h('div', { class: 'wn-body' }, h('div', { class: 'wn-label' }, 'NEW SINCE YOUR LAST UPDATE'), h('div', { class: 'wn-grid' }, ...cards)), h('div', { class: 'wn-foot' }, h('a', { class: 'wn-link', href: CHANGELOG_URL, target: '_blank', rel: 'noopener noreferrer' }, 'Full changelog', icon(I.external, 'wn-link-ic')), h('span', { class: 'wn-once' }, 'Shown once'), h('span', { class: 'grow' }), h('button', { class: 'primary', 'data-wn': 'close', onclick: deps.closeModal }, 'Got it'))));
}
//# sourceMappingURL=whatsnew.js.map