/** Bounded restart observations. Only start() may send one restart; check() is always read-only. */
import { ApiError, call } from './api.js';
export class RestartCheck {
    changed;
    request;
    now;
    delay;
    status = null;
    baseline = '';
    sawDown = false;
    active = false;
    rejected = false;
    uncertain = false;
    healthFinding = '';
    constructor(changed, request = call, now = () => performance.now(), delay = (ms) => new Promise(r => setTimeout(r, ms))) {
        this.changed = changed;
        this.request = request;
        this.now = now;
        this.delay = delay;
    }
    report(patch) {
        if (patch.finding)
            patch.history = [...(this.status?.history || []), { at: Date.now(), text: patch.finding,
                    tone: patch.phase === 'rejected' || patch.phase === 'timeout' ? 'bad' : patch.reachable === true ? 'ok' : patch.reachable === false ? 'warn' : '',
                    ...(patch.durationMs !== undefined ? { durationMs: patch.durationMs } : {}) }].slice(-4);
        if (patch.checking === false)
            patch.finishedAt = Date.now();
        this.status = { ...this.status, ...patch };
        this.changed({ ...this.status });
    }
    async health(timeout = 1500) {
        try {
            const r = await this.request('GET', '/api/ping', undefined, undefined, Math.max(1, Math.ceil(timeout)));
            if (r.data?.ok === true && typeof r.data.version === 'number')
                return r.data;
            this.healthFinding = 'The response did not identify a Piper service.';
            return null;
        }
        catch (e) {
            this.healthFinding = e instanceof ApiError ? e.status === 401 || e.status === 403
                ? 'Piper refused authentication.' : `Health check answered HTTP ${e.status}.`
                : 'No answer to the health check within 1.5 s, or connection unavailable.';
            return null;
        }
    }
    async start(route = '/api/restart') {
        if (this.active)
            return;
        this.active = true;
        this.baseline = '';
        this.sawDown = false;
        this.rejected = false;
        this.uncertain = false;
        this.status = { phase: 'requesting', startedAt: Date.now(), checkedAt: 0, checks: 0, checking: true,
            reachable: false, history: [], finding: 'Checking the current service before requesting one restart.' };
        this.report({});
        try {
            const began = this.now(), before = await this.health();
            this.baseline = before?.service_instance || '';
            this.sawDown = !before;
            this.report({ reachable: !!before, checks: 1, checkedAt: Date.now(), durationMs: this.now() - began,
                finding: before ? 'Original service health check answered.' : 'The original service did not answer the health check.' });
            try {
                await this.request('POST', route, {}, undefined, 1500);
            }
            catch (e) {
                if (e instanceof ApiError && e.status < 500) {
                    this.rejected = true;
                    this.report({ phase: 'rejected', checking: false, responseStatus: e.status,
                        rejection: e.status === 409 && e.message === 'Piper update is not ready: finish the merge before restarting.'
                            ? e.message : `The service refused the restart (HTTP ${e.status}).`, finding: e.status === 409
                            ? 'The update was rejected before restart. Finish or repair the update first; the running service was kept.'
                            : `Restart was refused (HTTP ${e.status}). No further restart was sent.` });
                    return;
                }
                this.uncertain = true;
                this.report({ phase: 'uncertain', checking: true,
                    finding: 'No restart acknowledgement. It may have been accepted; check availability before taking further action.' });
            }
        }
        finally {
            this.active = false;
        }
        await this.check();
    }
    async check() {
        if (this.active || !this.status)
            return;
        this.active = true;
        this.report({ phase: 'waiting', checking: true, finishedAt: undefined });
        const end = this.now() + 30000;
        try {
            for (let i = 0; i < 60 && this.now() < end; i++) {
                const began = this.now(), h = await this.health(Math.min(1500, end - this.now()));
                const verified = !!(h?.service_instance && this.baseline && h.service_instance !== this.baseline);
                const returned = !!h && this.sawDown && !this.baseline;
                this.sawDown ||= !h;
                this.report({ checkedAt: Date.now(), checks: this.status.checks + 1, reachable: !!h, durationMs: this.now() - began,
                    phase: this.rejected && h ? 'rejected' : verified ? 'recovered' : returned ? 'returned' : 'waiting',
                    finding: this.rejected && h ? 'The restart was refused. The original service is answering.'
                        : verified ? 'A new Piper service instance is answering.'
                            : returned ? 'Piper is answering again; this older service cannot confirm its restart identity.'
                                : h ? 'Piper is answering; a new service instance is not confirmed yet.'
                                    : this.healthFinding });
                if (verified || returned || (this.rejected && h))
                    return;
                const remaining = end - this.now();
                if (remaining > 0)
                    await this.delay(Math.min(500, remaining));
            }
            this.report({ phase: this.uncertain && this.status.reachable ? 'uncertain' : 'timeout', finding: this.status.reachable
                    ? this.uncertain ? 'No restart acknowledgement. Piper answers, but a new instance was not confirmed within 30 s.'
                        : 'Piper answers, but the restart was not confirmed within 30 s.' : 'Piper did not return within 30 s. Checks stopped.' });
        }
        finally {
            this.active = false;
            this.report({ checking: false });
        }
    }
}
/** Whitelisted observations only: no API bodies, session content, paths, URLs or credentials. */
export function restartDiagnostics(s) {
    return ['Piper restart diagnostics', `Outcome: ${s.phase}`, `Service answering: ${s.reachable ? 'yes' : 'no'}`,
        `Elapsed: ${Math.max(0, Math.floor(((s.finishedAt || Date.now()) - s.startedAt) / 1000))} s`, `Health checks: ${s.checks}`,
        `Last check: ${s.checkedAt ? new Date(s.checkedAt).toISOString() : 'none'}`, s.finding,
        'Check again only checks availability; it does not restart Piper.'].join('\n');
}
//# sourceMappingURL=restartstatus.js.map