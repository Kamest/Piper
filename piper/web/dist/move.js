/** Move to Server: presentation of the durable backend state, never an automatic write retry. */
import { call } from './api.js';
import { hiddenPaths } from './convo.js';
import { fi, h } from './dom.js';
import { hosts, refHost, scoped } from './environment.js';
export const movedReason = (s) => s?.moved ? 'Locked here. This session continues on Server.' : '';
export const moveServers = (rows = hosts.values()) => [...rows].filter(r => r.host.mode === 'service' && r.capabilities?.transfer === true);
export function moveDisabled(s, rows = hosts.values()) {
    if (refHost(s.id) || s.kind !== 'project' || !s.project)
        return 'Only local project sessions can be moved';
    if (s.moved)
        return movedReason(s);
    if (s.transfer)
        return s.transfer.phase === 'remote_unknown' ? 'Check on Server first' : s.transfer.status === 'halted' ? 'Continue the move from its status bar' : 'This session is being moved already';
    const servers = [...rows].filter(r => r.host.mode === 'service');
    if (!servers.length)
        return 'Add a Piper service in Settings > Servers first';
    if (!moveServers(servers).length)
        return servers.some(r => !r.capabilities)
            ? servers.some(r => r.online) ? 'Checking server capabilities…' : 'Server unavailable'
            : 'Update service first';
    return '';
}
export function moveView(t, sid) {
    if (t.phase === 'remote_unknown')
        return { tone: 'warn', text: 'Server did not answer – it may have started the session', detail: 'Piper never sends it twice by itself.', step: 4, action: 'check' };
    if (t.status === 'halted') {
        if (t.phase === 'pushed')
            return { tone: 'bad', text: `Server refused: ${t.error || 'request refused'}`, detail: 'Pushed. Nothing was created on Server.', step: 4, action: 'continue' };
        const declined = t.phase === 'committed' && /declin|denied|refused|reject/i.test(t.error ?? '');
        return { tone: 'bad', text: declined ? 'stopped: the push was declined' : `stopped: ${t.error || 'the move was interrupted'}`,
            detail: t.phase === 'committed' ? 'The WIP commit is here, nothing went to Server.' : undefined,
            step: t.phase === 'committed' ? 3 : t.phase === 'stopped' ? 2 : 0, action: 'continue' };
    }
    if (t.phase === 'waiting_send')
        return { tone: 'warn', text: 'you are typing in its prompt – the request waits', detail: 'Piper never sends into a half-written prompt.', step: 0, action: 'send' };
    if (t.phase === 'blocked_on_card')
        return { tone: 'warn', text: 'answer its card first – the request goes right after', step: 0, action: 'card' };
    if (['remote_sending', 'remote_created', 'moved'].includes(t.phase))
        return { tone: 'run', text: 'starting the session on Server…', step: 4 };
    if (['stopped', 'committed', 'pushed'].includes(t.phase))
        return { tone: 'run', text: `pushing wip: handoff ${sid} – confirm the push card`, step: 3 };
    if (t.phase === 'handoff_sent')
        return { tone: 'run', text: 'the agent is writing HANDOFF.md', step: 0 };
    return { tone: 'run', text: t.mode === 'wait' ? 'waiting for the agent to finish its turn' : 'asking the agent to wrap up', detail: 'Stop the session yourself to move on without HANDOFF.md.', step: 0 };
}
export function createMoveUI(deps) {
    const busy = new Set();
    const requests = new Map();
    const dismissed = new Set();
    const path = (s) => `/api/sessions/${encodeURIComponent(s.id)}/move`;
    const error = (e) => e instanceof Error ? e.message : String(e);
    async function run(s, operation) {
        if (busy.has(s.id))
            return;
        busy.add(s.id);
        deps.changed();
        try {
            await operation();
        }
        catch (e) {
            deps.toast(error(e));
        }
        finally {
            busy.delete(s.id);
            deps.changed();
        }
    }
    function hiddenConfirm(s, body, paths) {
        deps.modal('Move to Server?', h('div', { class: 'resume-hidden', role: 'alertdialog', 'aria-label': 'Conversation holds hidden file content' }, h('p', {}, h('b', {}, 'The conversation touched files hidden from agents. '), 'Their content may be in the handoff Server gets:'), paths.length ? h('ul', { class: 'st-warn' }, ...paths.map(p => h('li', {}, h('code', {}, p))))
            : h('p', { class: 'small' }, 'The isolation rule cannot be read, so the hidden files cannot be named.')), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); void start(s, { ...body, confirm_hidden: true }); } }, 'Move anyway')));
    }
    async function start(s, body) {
        await run(s, async () => {
            try {
                // The current controller returns the record; also accept the documented envelope.
                const r = await call('POST', path(s), body);
                const record = r.data && ('transfer' in r.data ? r.data.transfer : r.data);
                if (!record?.id || !record.phase)
                    throw new Error('Move outcome is not confirmed. Check its current state before moving again.');
                requests.set(s.id, { ...body });
                s.transfer = record;
            }
            catch (e) {
                const paths = hiddenPaths(e);
                if (paths && !body.confirm_hidden)
                    hiddenConfirm(s, body, paths);
                else
                    throw e;
            }
        });
    }
    async function confirm(s) {
        const why = moveDisabled(deps.session(s.id) ?? s);
        if (why)
            return deps.toast(why);
        const servers = moveServers();
        const body = { environment: servers[0].host.id, mode: 'wait' };
        const facts = h('div', { class: 'move-facts' }, h('p', { class: 'mut small' }, 'Reading branch and uncommitted files…'));
        const submit = h('button', { class: 'primary', disabled: true, onclick: () => {
                deps.closeModal();
                void start(s, { ...body });
            } }, 'Move to Server');
        const content = h('div', { class: 'move-dialog' }, h('p', {}, 'The agent writes HANDOFF.md and stops. Piper commits the work as a WIP commit, pushes it and starts a session on Server that continues from it. This session stays here, locked and resumable.'));
        if (servers.length > 1) {
            const select = h('select', { id: 'move-server', onchange: () => { body.environment = select.value; } }, ...servers.map(r => h('option', { value: r.host.id }, r.host.label)));
            content.append(h('div', { class: 'row' }, h('label', { for: 'move-server' }, 'Server'), select));
        }
        const choices = h('div', { class: 'sc-levels move-choices', role: 'radiogroup', 'aria-label': 'When to ask the agent to wrap up' });
        const radios = [];
        for (const [mode, title, text] of [
            ['wait', 'After its turn', 'Lets it finish what it is doing, then asks it to write HANDOFF.md.'],
            ['now', 'Now', 'Asks it at once, during its turn. Claude reads it after its next step, Codex when the turn ends. It is not stopped by force.'],
        ]) {
            const choose = () => { body.mode = mode; radios.forEach((b, i) => { const on = i === (mode === 'wait' ? 0 : 1); b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; }); };
            const radio = h('button', { type: 'button', class: 'sc-level' + (mode === 'wait' ? ' on' : ''), role: 'radio', 'aria-checked': String(mode === 'wait'), tabindex: mode === 'wait' ? 0 : -1,
                onclick: choose, onkeydown: (event) => {
                    const e = event;
                    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key))
                        return;
                    e.preventDefault();
                    const next = radios[mode === 'wait' ? 1 : 0];
                    next.click();
                    next.focus();
                } }, h('span', { class: 'sc-radio' }), h('span', { class: 'sc-level-body' }, h('b', {}, title), h('span', { class: 'mut small' }, text)));
            radios.push(radio);
            choices.append(radio);
        }
        content.append(h('p', { class: 'small', id: 'move-when' }, 'When to ask the agent to wrap up'), choices, facts);
        deps.modal('Move to Server?', content, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), submit));
        try {
            const r = await call('GET', `/api/sessions/${encodeURIComponent(s.id)}/git`);
            if (!r.data)
                throw new Error('Git status is unavailable');
            facts.replaceChildren(h('p', {}, 'Branch ', h('code', {}, r.data.branch || 'detached HEAD'), ` · ${r.data.files.length} uncommitted files go into the WIP commit`), h('p', { class: 'mut small' }, 'Stays here: .env and other ignored files, local DB and HTTP connections, Undo history. The push asks on its card as usual.'));
            submit.disabled = !r.data.branch;
            if (!r.data.branch)
                facts.append(h('p', { class: 'bad small' }, 'Check out a branch before moving this session.'));
        }
        catch (e) {
            facts.replaceChildren(h('p', { class: 'bad', role: 'alert' }, error(e)));
        }
    }
    function bar(s) {
        const moved = s.moved;
        const incoming = s.transferred_from;
        const t = s.transfer;
        let tone = 'run', title = '', text = '', detail, detailWarn = false, steps = null, action = null;
        const button = (label, go, primary = false) => h('button', { class: primary ? 'primary small' : 'small', disabled: busy.has(s.id), onclick: go }, label);
        if (moved) {
            tone = 'ok';
            title = 'Continues on Server';
            const target = scoped(moved.environment, 'session', moved.session);
            const time = new Date(moved.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
            text = `${deps.session(target)?.title || s.title} · since ${time}`;
            detail = `Locked here. Pull ${t?.branch || s.branch || 'the branch'} before you edit it on this computer.`;
            action = button('Open on Server', () => deps.open(target));
        }
        else if (t) {
            const v = moveView(t, s.id);
            tone = v.tone;
            title = 'Moving to Server';
            text = v.text;
            detail = v.detail;
            const done = [!!t.at.stopped, !!t.at.stopped, !!t.at.committed, !!t.at.pushed, !!t.at.remote_created];
            steps = h('ol', { class: 'move-steps', 'aria-label': 'Move progress' }, ...['Handoff', 'Stop', 'Commit', 'Push', 'Server'].map((label, i) => h('li', {
                class: done[i] ? 'done' : i === v.step ? 'current' : '', 'aria-current': !done[i] && i === v.step ? 'step' : undefined,
            }, h('span', { class: 'move-dot', 'aria-hidden': 'true' }), label)));
            if (v.action === 'send')
                action = button('Send now', () => void run(s, () => call('POST', `/api/sessions/${encodeURIComponent(s.id)}/input-clear`, {})), true);
            if (v.action === 'card')
                action = button('Show card', () => deps.showCard(s.id));
            if (v.action === 'continue')
                action = button('Continue move', () => void start(s, requests.get(s.id) ?? { environment: t.environment, mode: t.mode, remote_project: t.project }));
            if (v.action === 'check')
                action = button('Check on Server', () => void run(s, async () => {
                    await call('POST', path(s) + '/check', {});
                    const r = await call('GET', path(s));
                    if (r.data) {
                        s.transfer = r.data.transfer;
                        s.moved = r.data.moved ?? null;
                    }
                }));
        }
        else if (incoming && !dismissed.has(s.id + ':' + incoming.id)) {
            title = `Moved here from ${incoming.computer || 'another computer'}`;
            text = `${incoming.commit?.split(' ')[0] || 'unknown commit'} · HANDOFF.md in the project root`;
            // a quiet strip: only the manual-stop line is a warning (board 3b)
            if (incoming.manual_stop) {
                detailWarn = true;
                detail = 'The turn there was stopped by hand: no HANDOFF.md, the agent checks the state first.';
            }
            action = button('Dismiss', () => { dismissed.add(s.id + ':' + incoming.id); deps.changed(); });
        }
        else
            return null;
        return h('div', { class: `move-bar move-${tone}`, role: 'status', 'aria-label': title }, fi(tone === 'ok' ? 'check' : 'server'), h('div', { class: 'move-copy' }, h('div', {}, h('strong', {}, title), ' · ', text), detail ? h('div', { class: detailWarn ? 'warn small' : 'mut small' }, detail) : null), steps, action);
    }
    return { confirm, bar };
}
//# sourceMappingURL=move.js.map