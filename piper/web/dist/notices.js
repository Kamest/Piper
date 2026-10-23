// W13: every toast and desktop notification also lands here - IntelliJ's Notifications
// tool window. A ring of the last 100, an unread count for the bell in the activity bar. D11: "Recent alerts",
// kept across reloads in this browser (a store main.ts hands in); a restored one has no button - its action
// lived in the page that raised it. Pure: the GUI draws it (main.ts).
export const NOTICE_MAX = 100;
export const NOTICE_MAX_AGE = 24 * 60 * 60 * 1000;
export class NoticeLog {
    store;
    now;
    items = [];
    next = 1;
    stored;
    constructor(store, now = Date.now) {
        this.store = store;
        this.now = now;
        this.sync();
        this.prune();
    }
    /** Read another window's changes before any local mutation can overwrite them. */
    sync() {
        if (!this.store)
            return;
        try {
            const value = this.store.get();
            if (value === this.stored)
                return;
            const raw = JSON.parse(value ?? '[]');
            if (!Array.isArray(raw))
                return;
            const previous = this.items;
            this.items = raw.filter((x) => typeof x?.text === 'string' && typeof x.ts === 'number').slice(-NOTICE_MAX)
                .map((x) => {
                const old = previous.find((n) => n.ts === x.ts && n.text === x.text && n.src === String(x.src ?? 'toast') && n.sessionId === x.sessionId);
                return { id: old?.id ?? this.next++, ts: x.ts, text: x.text, src: String(x.src ?? 'toast'), action: old?.action, read: !!x.read, sessionId: typeof x.sessionId === 'string' ? x.sessionId : undefined };
            });
            this.stored = value;
        }
        catch { /* a broken or blocked store: keep this window's history */ }
    }
    /** Expire history on restore and use, including a page left open overnight. */
    prune() {
        const cutoff = this.now() - NOTICE_MAX_AGE;
        const kept = this.items.filter((n) => n.ts > cutoff);
        if (kept.length !== this.items.length) {
            this.items = kept;
            this.save();
        }
    }
    save() {
        try {
            const value = JSON.stringify(this.items.map(({ ts, text, src, read, sessionId }) => ({ ts, text, src, read, sessionId })));
            this.store?.set(value);
            this.stored = value;
        }
        catch { /* private window: lasts until reload */ }
    }
    /** `once`: a state, not an event - the same text moves to the top whenever it came, never piles up (N2) */
    add(text, src = 'toast', action, ts = this.now(), once = false, sessionId) {
        this.sync();
        this.prune();
        const duplicate = this.items.find((n) => n.text === text && n.src === src && n.sessionId === sessionId && ts >= n.ts && (once || ts - n.ts < 60_000));
        if (duplicate) {
            duplicate.ts = ts;
            duplicate.read = false;
            if (action)
                duplicate.action = action;
            this.items = this.items.filter((n) => n !== duplicate);
            this.items.push(duplicate);
            this.save();
            return duplicate;
        }
        const n = { id: this.next++, ts, text, src, action, read: false, sessionId };
        this.items.push(n);
        if (this.items.length > NOTICE_MAX)
            this.items.splice(0, this.items.length - NOTICE_MAX);
        this.save();
        return n;
    }
    /** newest first */
    list() {
        this.sync();
        this.prune();
        return [...this.items].reverse();
    }
    get unread() {
        this.sync();
        this.prune();
        return this.items.filter((n) => !n.read).length;
    }
    markRead() {
        this.sync();
        for (const n of this.items)
            n.read = true;
        this.save();
    }
    /** Remove only the entries shown in the panel; arrivals while it was open survive. */
    dismiss(ids) {
        const shown = this.items.filter((n) => ids.includes(n.id));
        this.sync();
        this.items = this.items.filter((n) => !shown.some((s) => s.ts === n.ts && s.text === n.text && s.src === n.src && s.sessionId === n.sessionId));
        this.save();
    }
    /** Viewing sessions acknowledges their history without touching other sessions or global notices. */
    dismissSessions(ids) {
        this.sync();
        const kept = this.items.filter((n) => !n.sessionId || !ids.includes(n.sessionId));
        if (kept.length === this.items.length)
            return false;
        this.items = kept;
        this.save();
        return true;
    }
    clear() {
        this.items = [];
        this.save();
    }
}
//# sourceMappingURL=notices.js.map