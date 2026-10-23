// Mattermost notifications: the GUI's presence heartbeat
// (piperd notifies only while nobody sees the GUI) and a small status/test dialog.
// piperd posts through an incoming webhook - one-way; answers from the phone need a bot account.
import { api } from './api.js';
import { ago, h } from './dom.js';
const BEAT_MS = 20000; // piperd treats 60 s without a "visible" beat as "not visible"
/** POST /api/presence while the page is visible (every 20 s and on every visibility change) */
export function startPresence() {
    const beat = () => void api.presence(document.visibilityState === 'visible').catch(() => undefined);
    document.addEventListener('visibilitychange', beat);
    window.addEventListener('pagehide', () => void api.presence(false).catch(() => undefined));
    window.setInterval(() => { if (document.visibilityState === 'visible')
        beat(); }, BEAT_MS);
    beat();
}
export function statusLines(st) {
    const out = [st.configured ? 'Set up: incoming webhook (notifications only).' : `Not set up: ${st.webhook_file} is missing.`];
    if (!st.configured)
        out.push('Save the webhook URL in Settings → Mattermost (or: sh scripts/piper.sh mattermost-webhook < file-with-url).');
    out.push(`Details in the message: ${st.detail === 'none' ? 'none' : st.detail === 'full' ? 'full' : 'the tool and the start of the answer'} · settings ${st.config_file}`);
    if (st.quiet)
        out.push('Quiet hours now: nothing is sent.');
    if (st.last_error)
        out.push(`Last error: ${st.last_error}`);
    else if (st.last_ok)
        out.push(`Last message sent ${ago(st.last_ok)}.`);
    out.push(st.note);
    return out;
}
/** F22: Settings › Mattermost's status line - connected / the last error / not set up; `channel` = the draft's override
 *  (the status has none), `null` = the status is still loading */
export function mmStatusLine(st, channel = '') {
    if (!st)
        return { tone: 'mut', text: 'Checking the connection…' };
    if (!st.configured)
        return { tone: 'mut', text: 'Not set up - paste the webhook URL below' };
    const quiet = st.quiet ? ' · quiet hours now' : '';
    if (st.last_error)
        return { tone: 'bad', text: `Last message failed: ${st.last_error}${quiet}` };
    const to = channel ? ` to ~${channel}` : '';
    if (st.last_ok) {
        const when = new Date(st.last_ok * 1000);
        const today = when.toDateString() === new Date().toDateString();
        const at = today ? when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : `${ago(st.last_ok)} ago`;
        return { tone: 'ok', text: `Connected · last message ${at}${to}${quiet}` };
    }
    return { tone: 'ok', text: `Webhook set · no message sent yet${to}${quiet}` };
}
/** F22: "Notify after" as minutes; a value off the segment stays and shows as its seconds (exact value in Advanced) */
export const MM_AFTER = [[60, '1 min'], [180, '3 min'], [600, '10 min']];
/** F22: the message detail as three short names; the full description is the tooltip */
export const MM_DETAIL = { none: 'Title only', command: '+ command', full: 'Full answer' };
/** F22: the Advanced line - "rate limit (1 per session / 15 min), channel, sender name" */
export function mmAdvancedSummary(gapS) {
    const gap = !gapS ? 'no rate limit' : gapS % 60 ? `rate limit (1 per session / ${gapS} s)` : `rate limit (1 per session / ${gapS / 60} min)`;
    return `Advanced — ${gap}, channel, sender name`;
}
export async function openMattermost(modal, toast) {
    const box = h('div', { class: 'mmstatus' });
    const draw = (st) => box.replaceChildren(...statusLines(st).map((l) => h('p', { class: 'small' }, l)));
    const test = h('button', {
        class: 'primary', onclick: async () => {
            test.disabled = true;
            try {
                const r = await api.mmTest();
                if (r.data)
                    draw(r.data);
                toast('Test message sent to Mattermost');
            }
            catch (e) {
                toast('Mattermost: ' + String(e instanceof Error ? e.message : e));
            }
            finally {
                test.disabled = false;
            }
        },
    }, 'Send a test message');
    modal('Mattermost', box, h('div', { class: 'row' }, test));
    try {
        const r = await api.mmStatus();
        if (r.data) {
            draw(r.data);
            test.disabled = !r.data.configured;
        }
    }
    catch (e) {
        box.replaceChildren(h('p', { class: 'warn small' }, String(e instanceof Error ? e.message : e)));
    }
}
//# sourceMappingURL=mm.js.map