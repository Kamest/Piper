// Phone pairing. Shared Piper controls and theme only.
import { fi, h } from './dom.js';
import { markBidi } from './text.js';
/** iOS browser and Home Screen app have separate identities. Existing paired browsers keep working. */
export function mobileBrowserInstall() {
    return !window.matchMedia('(display-mode: standalone)').matches && !navigator.standalone;
}
let stage = null;
let clock = 0;
let viewportBound = false;
/** Safari's keyboard and browser chrome resize the visible area, including non-terminal views. */
export function installMobileViewport() {
    if (viewportBound)
        return;
    viewportBound = true;
    const update = () => {
        const viewport = window.visualViewport;
        if (viewport && Math.abs(viewport.scale - 1) > .01)
            return; // preserve native pinch zoom/panning
        const height = viewport && viewport.height > 0 ? viewport.height : window.innerHeight;
        const top = Math.max(0, viewport?.offsetTop ?? 0);
        document.documentElement.style.setProperty('--mobile-viewport-height', `${height}px`);
        document.documentElement.style.setProperty('--mobile-viewport-top', `${top}px`);
    };
    window.addEventListener('resize', update);
    window.visualViewport?.addEventListener('resize', update);
    window.visualViewport?.addEventListener('scroll', update);
    update();
}
export function clearMobileState() {
    window.clearInterval(clock);
    stage?.remove();
    stage = null;
    const app = document.getElementById('app');
    if (app)
        app.inert = false;
}
export function showMobileState(state, computer = 'Piper', actions = {}, inGui = false, paired = true, browserOnly = false) {
    clearMobileState();
    const app = document.getElementById('app');
    if (app)
        app.inert = true;
    const button = (label, action, primary = false) => h('button', { ...(primary ? { class: 'primary' } : {}), onclick: () => action?.() }, label);
    const title = (text, icon = 'lock') => h('h2', {}, fi(icon), text);
    const step = (n, text) => h('div', { class: 'mobile-step' }, h('span', {}, String(n)), h('div', {}, text));
    const guiPath = () => h('b', { class: 'mobile-gui-path' }, 'Settings › Mobile access › Pair phone');
    const paste = (app = false) => {
        const input = h('textarea', { id: 'mobile-link', rows: '3', maxlength: '4096', autocomplete: 'off', autocorrect: 'off', autocapitalize: 'none', inputmode: 'url', spellcheck: 'false', placeholder: 'Paste the link copied on the computer' });
        const error = h('p', { class: 'bad small', role: 'alert', hidden: true });
        const next = h('button', { class: 'primary', disabled: true, onclick: async () => {
                if (!actions.openLink)
                    return;
                next.disabled = true;
                const value = input.value.trim();
                input.value = '';
                try {
                    const problem = await actions.openLink(value);
                    if (problem) {
                        error.textContent = markBidi(problem);
                        error.hidden = false;
                        input.focus();
                    }
                }
                catch {
                    error.textContent = 'Couldn’t open this link. Copy a new one on the computer.';
                    error.hidden = false;
                }
            } }, 'Continue');
        input.addEventListener('input', () => {
            const value = input.value.trim(), valid = actions.validLink?.(value) ?? false;
            next.disabled = !valid;
            error.hidden = !value || valid;
            error.textContent = 'This isn’t a Piper pairing link. Copy it again on the computer.';
            input.setAttribute('aria-invalid', String(!!value && !valid));
        });
        return h('div', { class: 'mobile-paste' }, app ? null : h('div', { class: 'mobile-or' }, 'or paste the link'), h('label', { for: 'mobile-link' }, 'Pairing link'), input, app ? null : h('p', { class: 'mut small' }, 'On the computer choose Copy pairing link, then paste it here.'), error, next);
    };
    const apple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const card = h('section', { class: 'modal', role: 'dialog', 'aria-modal': 'true', tabindex: '-1' });
    if (state === 'install') {
        card.append(title('Add Piper to your Home Screen', 'phone'), h('p', { class: 'mut' }, 'Piper works best as an app on your Home Screen. You’ll pair it there, once.'), step(1, apple ? 'Tap Share in this browser.' : 'Open the browser menu.'), ...(apple ? [h('p', { class: 'mut small mobile-gui-path' }, 'In Chrome it’s next to the address bar. If you don’t see it, tap More first. No need to switch browsers.')] : []), step(2, apple ? 'Choose Add to Home Screen.' : 'Choose Add to Home screen or Install app.'), ...(apple ? [h('p', { class: 'mut small mobile-gui-path' }, 'If Open as Web App is shown, leave it on.')] : []), step(3, 'Open Piper from the Home Screen and pair it there.'), h('p', { class: 'mut small' }, 'Nothing is paired in this browser. The Home Screen app keeps its own sign-in.'), h('div', { class: 'mobile-or' }, 'or'), button('Use Piper in this browser instead', actions.browser));
    }
    else if (state === 'unpaired') {
        const browser = mobileBrowserInstall();
        card.append(title(browser ? 'Pair this browser with Piper' : 'Pair Piper with your computer', 'phone'), h('p', { class: 'mut' }, browser ? 'Piper opens only on a phone paired from your computer.' : 'Piper opens only on a phone paired from your computer. You pair this app once.'), ...(browser ? [h('div', { class: 'mobile-where' }, h('strong', {}, 'Pairs only this browser'), h('p', { class: 'mut small' }, 'Each browser and the Home Screen app keep their own pairing. A pairing made here won’t carry over to the Home Screen app.'), button('Add to Home Screen instead', actions.install))] : []), step(1, 'On the computer, open'), guiPath(), step(2, browser ? 'Choose Use in a browser only and scan the QR code with this phone’s camera.' : 'Choose Next: pair the app, then Copy pairing link.'), ...(browser ? [] : [step(3, 'Paste the link here.')]), paste(!browser), h('p', { class: 'mut small' }, browser ? 'Each code works once.' : 'A link works once, for 60 seconds. Don’t scan it with the Camera app: that opens a browser, not this app.'));
    }
    else if (state === 'pair') {
        const browser = mobileBrowserInstall();
        card.append(title(browser ? `Pair this browser with ${computer}?` : `Pair with ${computer}?`), h('p', { class: 'mobile-verified mut' }, fi('check'), 'Computer verified · end-to-end encrypted'), browser && !browserOnly ? h('div', { class: 'mobile-where' }, h('strong', {}, 'Want the Home Screen app instead?'), h('p', { class: 'mut small' }, 'This pairs only this browser: the Home Screen app won’t get it. Tap Cancel, open Piper from the Home Screen and paste the link there.')) : document.createTextNode(''), h('div', { class: 'protected-callout' }, h('strong', {}, 'Full access to Piper'), h('p', {}, `${browser ? 'This browser' : 'This phone'} can do everything you can do in Piper on this computer:`), h('ul', {}, h('li', {}, 'start, stop and type into agent sessions and host terminals'), h('li', {}, 'read and change project files and databases'), h('li', {}, 'approve agent requests, commit and push')), h('p', { class: 'mut' }, 'Anyone who can unlock this phone gets the same access. Remove it on the computer in Settings › Mobile access.')), h('div', { class: 'row mobile-pair-actions' }, button('Cancel', actions.cancel), button('Pair with full access', actions.pair, true)));
    }
    else if (state === 'connecting' || state === 'pairing') {
        card.className = 'progress';
        const time = h('span', { class: 'startup-clock' }, '0:00');
        const start = Date.now();
        clock = window.setInterval(() => {
            const seconds = Math.floor((Date.now() - start) / 1000);
            time.textContent = markBidi(`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`);
        }, 1000);
        const pairing = state === 'pairing';
        card.append(h('div', { class: 'startup-head' }, h('span', { class: 'spinner' }), h('div', { class: 'startup-heading' }, h('div', { class: 'startup-eyebrow' }, pairing ? 'PAIRING' : 'CONNECTING'), h('strong', {}, computer)), time), h('p', { class: 'startup-hint' }, pairing ? "Sending this phone's key. Keep this page open." : 'End-to-end encrypted through the relay. The relay cannot read your session.'), h('ol', { class: 'startup-steps' }, ...[pairing ? 'Computer verified' : 'Relay', pairing ? 'Phone added' : 'Computer verified', 'Piper']
            .map((name, i) => h('li', { class: 'step ' + (i === 0 ? 'done' : i === 1 ? 'now' : '') }, name))));
    }
    else if (state === 'expired') {
        card.append(title('This pairing link is no longer valid'), h('p', { class: 'mut' }, 'A link works for 60 seconds and only once. Nothing was paired.'), step(1, 'On the computer, choose Generate new link or open Settings › Mobile access › Pair phone again.'), step(2, 'Copy the new link and paste it here right away.'), paste());
    }
    else if (state === 'revoked') {
        if (inGui)
            card.className = 'panel bad mobile-revoked';
        card.append(title('Access removed', 'alert'), h('p', {}, `This phone was removed in Settings › Mobile access on ${computer}. The connection is closed.`), h('p', { class: 'mut' }, 'To use Piper here again, pair it on the computer: Settings › Mobile access › Pair phone.'), h('div', { class: 'row' }, button(`Forget ${computer}`, actions.forget)));
    }
    else if (state === 'identity') {
        card.append(title('Not connected', 'alert'), h('div', { class: 'protected-callout' }, h('strong', {}, "Computer identity doesn't match"), h('p', {}, `The computer that answered is not the ${computer} this phone was paired with. Nothing was sent.`), h('p', { class: 'mut' }, 'If Piper was reinstalled or its identity was reset, forget this computer and pair again with a fresh link.')), h('div', { class: 'row' }, button(`Forget ${computer}`, actions.forget)));
    }
    else {
        let remaining = 8;
        const retry = h('span', { class: 'mut' }, 'Retrying in 0:08');
        clock = window.setInterval(() => {
            remaining -= 1;
            retry.textContent = markBidi(`Retrying in 0:${String(remaining).padStart(2, '0')}`);
            if (remaining <= 0) {
                window.clearInterval(clock);
                actions.retry?.();
            }
        }, 1000);
        card.append(title(`Can't reach ${computer}`), h('p', { class: 'mut' }, 'The computer is asleep or offline, or Piper is not running on it. '
            + (paired ? 'This phone stays paired.' : 'Piper opens only on a phone paired from your computer.')), h('div', { class: 'row' }, retry, button('Retry now', actions.retry)));
    }
    card.setAttribute('aria-label', markBidi(card.querySelector('h2, strong')?.textContent ?? 'Mobile connection'));
    stage = h('div', { class: 'mobile-stage' + (inGui ? ' mobile-over-gui' : ''), 'data-state': state }, card);
    document.body.append(stage);
    card.focus({ preventScroll: true });
}
export function mobileOffline(on) {
    let banner = document.getElementById('mobile-offline');
    const app = document.getElementById('app');
    if (app)
        app.inert = on || stage !== null;
    if (!on) {
        banner?.remove();
        return;
    }
    if (!banner) {
        banner = h('div', { id: 'mobile-offline', role: 'alert' }, 'Connection lost · reconnecting. Actions sent just before may already have run; check before repeating them.');
        document.body.prepend(banner);
    }
}
//# sourceMappingURL=mobileui.js.map