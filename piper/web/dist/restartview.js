import { markBidi } from './text.js';
function node(tag, cls = '', text = '') {
    const el = document.createElement(tag);
    el.className = cls;
    el.textContent = markBidi(text);
    return el;
}
export function restartSummary(s) {
    return ['Piper restart diagnostics', `Outcome: ${s.phase}`, `Service answering: ${s.reachable ? 'yes' : 'no'}`,
        `Elapsed: ${Math.max(0, Math.floor(((s.finishedAt || Date.now()) - s.startedAt) / 1000))} s`, `Health checks: ${s.checks}`,
        `Last check: ${s.checkedAt ? new Date(s.checkedAt).toISOString() : 'none'}`, s.finding,
        'Read-only checks: at most 1.5 s each, 30 s per check cycle.',
        'Check again only checks availability; it does not restart Piper.'].join('\n');
}
export function createRestartView(actions) {
    const el = node('div', 'restart-stage'), card = node('section', 'restart-card');
    card.setAttribute('aria-label', 'Piper restart diagnostics');
    const body = node('div', 'restart-body'), head = node('div', 'restart-head');
    const mark = node('span', 'restart-mark'), heading = node('div', 'restart-heading');
    mark.setAttribute('aria-hidden', 'true');
    const title = node('h1'), sub = node('div', 'restart-sub'), chip = node('span', 'restart-chip');
    title.id = 'restart-title';
    card.setAttribute('aria-labelledby', title.id);
    heading.append(title, sub);
    const close = node('button', 'restart-close', '×');
    close.setAttribute('aria-label', 'Close restart diagnostics');
    close.onclick = actions.close;
    head.append(mark, heading, chip, close);
    const message = node('p', 'restart-message'), facts = node('div', 'restart-facts');
    message.id = 'restart-observation';
    card.setAttribute('aria-describedby', message.id);
    message.setAttribute('role', 'status');
    message.setAttribute('aria-live', 'polite');
    const rejection = node('div', 'restart-rejection'), rejectionLabel = node('div', 'restart-label', 'Answer from the service'), rejectionText = node('strong');
    rejection.append(rejectionLabel, rejectionText);
    rejection.hidden = true;
    const keys = [];
    const values = ['Elapsed', 'Checks', 'Last check', 'Next check'].map(label => {
        const fact = node('div'), key = node('div', 'restart-label', label), value = node('strong');
        fact.append(key, value);
        facts.append(fact);
        return value;
    });
    for (const fact of facts.children)
        keys.push(fact.firstElementChild);
    const latest = node('div', 'restart-latest'), label = node('div', 'restart-label', 'Latest checks'), rows = node('div');
    latest.append(label, rows);
    const hint = node('p', 'restart-hint', 'Check again only repeats the checks. It does not restart Piper.');
    const details = node('pre', 'restart-details');
    details.hidden = true;
    const feedback = node('div', 'restart-feedback');
    feedback.setAttribute('role', 'status');
    body.append(head, message, rejection, facts, latest, details, hint, feedback);
    const footer = node('div', 'restart-footer'), show = node('button', '', 'Show details'), spacer = node('span', 'restart-spacer');
    const copy = node('button', '', 'Copy diagnostics'), again = node('button', '', 'Check again'), primary = node('button', 'primary');
    again.onclick = actions.check;
    again.hidden = true;
    show.setAttribute('aria-expanded', 'false');
    show.onclick = () => { details.hidden = !details.hidden; show.textContent = details.hidden ? 'Show details' : 'Hide details'; show.setAttribute('aria-expanded', String(!details.hidden)); };
    let status = null;
    copy.onclick = () => {
        if (!status)
            return;
        void Promise.resolve().then(() => actions.copy(restartSummary(status))).then(() => { feedback.textContent = 'Diagnostics copied.'; }, () => {
            details.hidden = false;
            show.textContent = 'Hide details';
            show.setAttribute('aria-expanded', 'true');
            feedback.textContent = 'Copy unavailable. Select the diagnostic text in details to copy it.';
        });
    };
    primary.onclick = () => { if (status?.reachable && !status.checking)
        actions.close();
    else
        actions.check(); };
    footer.append(show, spacer, copy, again, primary);
    card.append(body, footer);
    el.append(card);
    el.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            actions.close();
        }
    });
    const clock = (at) => at ? new Date(at).toLocaleTimeString('en-GB', { hour12: false }) : '—';
    function tick() {
        if (!status)
            return;
        const seconds = Math.max(0, Math.floor(((status.finishedAt || Date.now()) - status.startedAt) / 1000));
        values[0].textContent = markBidi(status.phase === 'rejected' ? String(status.responseStatus || 'CLI')
            : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`);
    }
    const timer = setInterval(tick, 250);
    return { el, dispose: () => clearInterval(timer), update(s) {
            const first = !status;
            status = s;
            tick();
            const success = s.phase === 'recovered' || s.phase === 'returned', bad = s.phase === 'timeout', rejected = s.phase === 'rejected';
            card.dataset.phase = s.phase;
            card.dataset.tone = success ? 'ok' : bad || rejected ? 'bad' : s.phase === 'uncertain' || (!s.reachable && s.phase !== 'requesting') ? 'warn' : 'wait';
            title.textContent = markBidi(success ? 'Piper is back' : rejected ? 'Restart was not started' : bad ? (s.reachable ? 'Restart not confirmed' : 'Piper did not come back')
                : s.phase === 'uncertain' ? 'Restart not confirmed' : s.phase === 'requesting' ? 'Asking Piper to restart' : s.reachable ? 'Restarting Piper' : 'Waiting for the new service');
            chip.textContent = success ? 'Online' : rejected ? 'Rejected' : bad ? 'Timed out' : s.phase === 'uncertain' ? 'Unconfirmed'
                : s.phase === 'requesting' ? 'Requesting' : s.reachable ? 'Waiting' : 'Not answering';
            mark.textContent = success ? '✓' : rejected || bad || s.phase === 'uncertain' ? '!' : '…';
            sub.textContent = markBidi(s.phase === 'requesting' ? 'Preparing one restart request' : `Started ${clock(s.startedAt)} · ${s.reachable ? 'service answering' : 'service not answering'}`);
            message.textContent = markBidi(s.finding);
            rejection.hidden = !rejected;
            rejectionText.textContent = markBidi(s.rejection || 'The service refused the restart.');
            keys[0].textContent = rejected ? 'Answer' : 'Elapsed';
            keys[3].textContent = rejected ? 'Service' : 'Next check';
            values[1].textContent = markBidi(String(s.checks));
            values[2].textContent = markBidi(clock(s.checkedAt));
            values[3].textContent = markBidi(rejected ? s.reachable ? 'Answering' : 'Not answering' : s.checking ? 'Checking…' : 'Stopped');
            rows.replaceChildren(...s.history.slice().reverse().map(e => {
                const row = node('div', 'restart-check'), time = node('time', '', clock(e.at)), dot = node('span', 'restart-dot'), text = node('span', 'restart-check-text', e.text);
                row.dataset.tone = e.tone || '';
                text.title = markBidi(e.text);
                const duration = node('span', 'restart-duration', e.durationMs === undefined ? '' : `${Math.round(e.durationMs)} ms`);
                row.append(time, dot, text, duration);
                return row;
            }));
            if (!s.history.length)
                rows.append(node('div', 'restart-empty', 'The first check is in progress.'));
            details.textContent = markBidi(restartSummary(s));
            primary.disabled = false;
            primary.className = s.checking ? '' : 'primary';
            primary.textContent = s.checking ? 'Hide' : s.reachable ? 'Return to workspace' : 'Check again';
            again.hidden = s.phase !== 'uncertain';
            // An answering old instance can still need another observation cycle.
            if (!s.checking && !success && !rejected && s.reachable) {
                primary.textContent = 'Check again';
                primary.onclick = actions.check;
            }
            else
                primary.onclick = () => { if (status?.checking || status?.reachable)
                    actions.close();
                else
                    actions.check(); };
            if (first)
                primary.focus({ preventScroll: true });
        } };
}
//# sourceMappingURL=restartview.js.map