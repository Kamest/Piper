// W13: every toast and desktop notification also lands here - IntelliJ's Notifications
// tool window. A ring of the last 100, an unread count for the bell in the activity bar. D11: "Recent alerts",
// kept across reloads in this browser (a store main.ts hands in); a restored one has no button - its action
// lived in the page that raised it. Pure: the GUI draws it (main.ts).

export interface Notice {
  id: number;
  ts: number;
  text: string;
  /** 'toast' | 'desktop' | 'files' */
  src: string;
  action?: { label: string; run(): void } | undefined;
  read: boolean;
  /** Stable session identity, also kept across reloads. */
  sessionId?: string | undefined;
}

export const NOTICE_MAX = 100;
export const NOTICE_MAX_AGE = 24 * 60 * 60 * 1000;

/** where the ring survives a reload (localStorage in the GUI); get() may return null or throw */
export interface NoticeStore {
  get(): string | null;
  set(value: string): void;
}

export class NoticeLog {
  private items: Notice[] = [];
  private next = 1;

  private stored: string | null | undefined;

  constructor(private store?: NoticeStore, private now: () => number = Date.now) {
    this.sync();
    this.prune();
  }

  /** Read another window's changes before any local mutation can overwrite them. */
  private sync(): void {
    if (!this.store) return;
    try {
      const value = this.store.get();
      if (value === this.stored) return;
      const raw = JSON.parse(value ?? '[]') as Partial<Notice>[];
      if (!Array.isArray(raw)) return;
      const previous = this.items;
      this.items = raw.filter((x) => typeof x?.text === 'string' && typeof x.ts === 'number').slice(-NOTICE_MAX)
        .map((x) => {
          const old = previous.find((n) => n.ts === x.ts && n.text === x.text && n.src === String(x.src ?? 'toast') && n.sessionId === x.sessionId);
          return { id: old?.id ?? this.next++, ts: x.ts!, text: x.text!, src: String(x.src ?? 'toast'), action: old?.action, read: !!x.read, sessionId: typeof x.sessionId === 'string' ? x.sessionId : undefined };
        });
      this.stored = value;
    } catch { /* a broken or blocked store: keep this window's history */ }
  }

  /** Expire history on restore and use, including a page left open overnight. */
  private prune(): void {
    const cutoff = this.now() - NOTICE_MAX_AGE;
    const kept = this.items.filter((n) => n.ts > cutoff);
    if (kept.length !== this.items.length) {
      this.items = kept;
      this.save();
    }
  }

  private save(): void {
    try {
      const value = JSON.stringify(this.items.map(({ ts, text, src, read, sessionId }) => ({ ts, text, src, read, sessionId })));
      this.store?.set(value);
      this.stored = value;
    } catch { /* private window: lasts until reload */ }
  }

  /** `once`: a state, not an event - the same text moves to the top whenever it came, never piles up (N2) */
  add(text: string, src = 'toast', action?: { label: string; run(): void }, ts = this.now(), once = false, sessionId?: string): Notice {
    this.sync();
    this.prune();
    const duplicate = this.items.find((n) => n.text === text && n.src === src && n.sessionId === sessionId && ts >= n.ts && (once || ts - n.ts < 60_000));
    if (duplicate) {
      duplicate.ts = ts;
      duplicate.read = false;
      if (action) duplicate.action = action;
      this.items = this.items.filter((n) => n !== duplicate);
      this.items.push(duplicate);
      this.save();
      return duplicate;
    }
    const n: Notice = { id: this.next++, ts, text, src, action, read: false, sessionId };
    this.items.push(n);
    if (this.items.length > NOTICE_MAX) this.items.splice(0, this.items.length - NOTICE_MAX);
    this.save();
    return n;
  }

  /** newest first */
  list(): Notice[] {
    this.sync();
    this.prune();
    return [...this.items].reverse();
  }

  get unread(): number {
    this.sync();
    this.prune();
    return this.items.filter((n) => !n.read).length;
  }

  markRead(): void {
    this.sync();
    for (const n of this.items) n.read = true;
    this.save();
  }

  /** Remove only the entries shown in the panel; arrivals while it was open survive. */
  dismiss(ids: number[]): void {
    const shown = this.items.filter((n) => ids.includes(n.id));
    this.sync();
    this.items = this.items.filter((n) => !shown.some((s) => s.ts === n.ts && s.text === n.text && s.src === n.src && s.sessionId === n.sessionId));
    this.save();
  }

  /** Viewing sessions acknowledges their history without touching other sessions or global notices. */
  dismissSessions(ids: string[]): boolean {
    this.sync();
    const kept = this.items.filter((n) => !n.sessionId || !ids.includes(n.sessionId));
    if (kept.length === this.items.length) return false;
    this.items = kept;
    this.save();
    return true;
  }

  clear(): void {
    this.items = [];
    this.save();
  }
}
