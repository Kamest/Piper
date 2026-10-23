// Compact Claude/Codex plan usage below the queue and in its collapsed strip.
// piperd polls each provider independently. Resets and freshness live in the accessible peek;
// failed or elapsed windows keep their last known values, dimmed until a fresh answer arrives.
import { ago, h } from './dom.js';
import type { LimitsSummary } from './types.js';
import { markBidi } from './text.js';

export type Tone = 'ok' | 'warn' | 'bad';

/** colour = state: link below 75 %, amber to 99 %, red at 100 % (or when the API says so) */
export function tone(percent: number | null, severity = 'normal'): Tone {
  if (percent !== null && percent >= 100) return 'bad';
  if (severity !== 'normal' && severity !== '') return percent !== null && percent >= 100 ? 'bad' : 'warn';
  return percent !== null && percent >= 75 ? 'warn' : 'ok';
}

const pad = (n: number) => String(n).padStart(2, '0');

/** "in 3h 41m" / "in 42m" to a reset; "resetting…" once it passed (fresh numbers follow on the next poll) */
export function countdown(resetsAt: number | null, now: number): string {
  if (resetsAt === null) return '';
  const s = Math.floor(resetsAt - now);
  if (s <= 0) return 'resetting…';
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  if (hh === 0) return mm === 0 ? `in ${s}s` : `in ${mm}m`;
  if (hh >= 24) return `in ${Math.floor(hh / 24)}d ${hh % 24}h`;
  return `in ${hh}h ${pad(mm)}m`;
}

/** the weekly reset: weekday + local time · how far ("Thu 02:00 · 1d 9h") */
export function weekText(resetsAt: number | null, now: number): string {
  if (resetsAt === null) return '';
  const s = Math.floor(resetsAt - now);
  if (s <= 0) return 'resetting…';
  const d = new Date(resetsAt * 1000);
  const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
  const far = s >= 86400 ? `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h` : s >= 3600 ? `${Math.floor(s / 3600)}h ${pad(Math.floor((s % 3600) / 60))}m` : `${Math.floor(s / 60)}m`;
  return `${day} ${pad(d.getHours())}:${pad(d.getMinutes())} · ${far}`;
}

export interface WeekRow {
  /** the all-models weekly limit; model-scoped limits belong in the detail */
  percent: number | null;
  resetsAt: number | null;
  severity: string;
  all: number | null;
  scoped: { label: string; percent: number | null }[];
}

/** The Week summary always represents the all-models window. */
export function weekRow(s: LimitsSummary): WeekRow {
  const all = s.week ?? null;
  return { percent: all?.percent ?? null, resetsAt: all?.resets_at ?? null, severity: all?.severity ?? 'normal',
    all: all?.percent ?? null, scoped: (s.scoped ?? []).map((x) => ({ label: x.label, percent: x.percent })) };
}

/** the window rolled over since piperd last asked: keep last known values dimmed */
export function rolledOver(resetsAt: number | null, fetchedAt: number, now: number): boolean {
  return resetsAt !== null && now > resetsAt && fetchedAt < resetsAt;
}

const pct = (p: number | null) => (p === null ? '– %' : `${p} %`);

export interface LimitsDeps {
  /** the strip tile was clicked: the whole queue */
  open(): void;
  /** hover on the strip tile: the peek next to it, and away */
  peek(nodes: Node[], anchor: HTMLElement): void;
  hidePeek(): void;
  /** the tile appeared or went: the strip fits its tiles again (+N) */
  refit?(): void;
}

/** One compact row per provider; detailed resets live in the existing queue peek. */
export function createLimits(deps: LimitsDeps) {
  const el = h('div', { class: 'q-limits', 'aria-label': 'Plan usage' });
  const tile = h('div', { class: 'qm-limits' });
  const columns = h('div', { class: 'ql-columns' }, h('span', {}, 'Used'), h('span', {}, 'Session'), h('span', {}, 'Week'));
  el.append(columns);
  el.hidden = tile.hidden = true;
  let refreshPeek: (() => void) | null = null;
  let peekAnchor: HTMLElement | null = null;
  const hide = () => { refreshPeek = null; peekAnchor = null; deps.hidePeek(); };
  const providers = (['Claude', 'Codex'] as const).map((name) => {
    const label = h('span', { class: 'ql-provider-name' }, name);
    const indicator = h('span', { class: 'ql-state', 'aria-hidden': 'true' }, '·');
    const percent = [h('span', { class: 'ql-pct' }), h('span', { class: 'ql-pct' })] as const;
    const fills = [h('i', { class: 'ql-fill' }), h('i', { class: 'ql-fill' })] as const;
    const bars = fills.map((fill) => h('span', { class: 'ql-bar', 'aria-hidden': 'true' }, fill));
    const cells = percent.map((p, i) => h('span', { class: 'ql-cell' }, p, bars[i]));
    const note = h('span', { class: 'ql-note', hidden: true });   // 9.121: "login expired" instead of two empty bars
    const row = h('button', { class: 'ql-provider', type: 'button', 'data-provider': name.toLowerCase() },
      h('span', { class: 'ql-name' }, label, indicator), ...cells, note);
    const miniPct = h('span', { class: 'qml-pct' });
    const miniFills = [h('b'), h('b')] as const;
    const mini = h('button', { class: 'qm-lim', type: 'button', 'data-provider': name.toLowerCase() },
      h('span', { class: 'qml-name' }, name === 'Claude' ? 'CL' : 'CX'), miniPct,
      ...miniFills.map((fill) => h('i', { class: 'qml-bar' }, fill)),
      h('i', { class: 'fi fi-alert qml-warn', 'aria-hidden': 'true' }));
    row.hidden = mini.hidden = true;
    el.append(row); tile.append(mini);
    const provider = { name, row, mini, indicator, percent, cells, note, fills, miniPct, miniFills, current: null as LimitsSummary | null };
    const show = (anchor: HTMLElement) => {
      peekAnchor = anchor;
      refreshPeek = () => { if (provider.current) deps.peek(detailNodes(name, provider.current, Date.now() / 1000), anchor); };
      refreshPeek();
    };
    for (const anchor of [row, mini]) {
      anchor.addEventListener('mouseenter', () => show(anchor));
      anchor.addEventListener('focus', () => show(anchor));
      anchor.addEventListener('mouseleave', hide);
      anchor.addEventListener('blur', hide);
      anchor.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
    }
    row.addEventListener('click', () => show(row));
    mini.addEventListener('click', () => deps.open());
    return provider;
  });

  function tick(): void {
    const now = Date.now() / 1000;
    for (const p of providers) {
      const s = p.current;
      if (!s) continue;
      const w = weekRow(s);
      const values = [s.session?.percent ?? null, w.percent] as const;
      const stale = s.status !== 'ok' || now - s.fetched_at >= 2400 ||
        [s.session?.resets_at, w.resetsAt].some((at) => at != null && rolledOver(at, s.fetched_at, now));
      p.row.classList.toggle('dim', stale); p.mini.classList.toggle('dim', stale);
      p.indicator.dataset.tone = s.status === 'expired' || s.status === 'error' ? 'warn' : '';
      const unknown = s.status !== 'ok' && values[0] === null && values[1] === null;
      p.indicator.hidden = !stale || unknown;   // the note already says it
      p.note.hidden = !unknown;
      p.note.textContent = unknown ? markBidi(noteText(s)) : '';
      for (const c of p.cells) c.hidden = unknown;
      p.mini.classList.toggle('unknown', unknown);
      for (const i of [0, 1] as const) {
        const value = values[i];
        const t = tone(value, i === 0 ? s.session?.severity : w.severity);
        p.percent[i].textContent = markBidi(pct(value));
        p.percent[i].dataset.tone = t;
        for (const bar of [p.fills[i], p.miniFills[i]]) {
          bar.style.width = `${Math.max(0, Math.min(100, value ?? 0))}%`;
          bar.dataset.tone = t;
        }
      }
      const miniValue = values[0] ?? values[1];
      p.miniPct.textContent = markBidi(`${miniValue ?? '–'}%`);
      p.miniPct.dataset.tone = tone(miniValue, values[0] === null ? w.severity : s.session?.severity);
      const description = unknown ? `${p.name} · ${noteText(s)}` : `${p.name} · Session ${pct(values[0])}, ${countdown(s.session?.resets_at ?? null, now)} · Week ${pct(values[1])}, ${weekText(w.resetsAt, now)} · ${statusText(s)}`;
      p.row.setAttribute('aria-label', markBidi(description));
      p.mini.setAttribute('aria-label', markBidi(description));
    }
    if (peekAnchor && (!peekAnchor.isConnected || !peekAnchor.getClientRects().length)) hide();
    refreshPeek?.();
  }

  window.setInterval(tick, 1000);
  return { el, tile, tick,
    sync(claude: LimitsSummary | null | undefined, codex?: LimitsSummary | null): void {
      const before = providers.filter((p) => !p.mini.hidden).length;
      let changed = false;
      providers.forEach((p, i) => {
        const s = i === 0 ? claude : codex;
        const visible = !!s && s.status !== 'no-login';
        changed ||= p.row.hidden === visible;
        p.current = visible ? s : null;
        p.row.hidden = p.mini.hidden = !visible;
      });
      const count = providers.filter((p) => !p.mini.hidden).length;
      el.hidden = tile.hidden = count === 0;
      if (changed) hide();
      if (before !== count) deps.refit?.();
      tick();
    },
  };
}

/** 9.121: the row's text when there is no number to show */
export function noteText(s: LimitsSummary): string {
  return s.status === 'expired' ? 'login expired' : s.error || 'usage unavailable';
}

export function statusText(s: LimitsSummary): string {
  const when = s.fetched_at ? ` · updated ${ago(s.fetched_at)}` : '';
  if (s.status === 'expired') return `login expired${when}`;
  if (s.status === 'error' || s.status === 'unavailable') return `${s.error || 'usage unavailable'}${when}`;
  return `${s.plan || 'plan'}${when}`;
}

export function detailNodes(name: string, s: LimitsSummary, now: number): Node[] {
  const row = (label: string, value: number | null, reset: string) => h('div', { class: 'qp-lim' },
    h('span', {}, label), h('b', {}, pct(value)), h('span', { class: 'grow' }), h('span', { class: 'qp-reset' }, reset));
  const result: Node[] = [h('div', { class: 'qp-title' }, `${name} limits${s.plan ? ' · ' + s.plan : ''}`),
    row('Session', s.session?.percent ?? null, countdown(s.session?.resets_at ?? null, now)),
    row('Week', s.week?.percent ?? null, weekText(s.week?.resets_at ?? null, now)),
    ...(s.scoped ?? []).map((w) => row(`Week · ${w.label}`, w.percent, weekText(w.resets_at, now))),
    h('div', { class: 'qp-keys' }, statusText(s))];
  if (s.status === 'expired') result.push(h('div', { class: 'qp-detail' }, `Run ${name === 'Codex' ? 'codex login' : 'claude'} on this machine to renew the login.`));
  if (s.status !== 'ok' && s.fetched_at) result.push(h('div', { class: 'qp-detail' }, 'Showing the last successful update.'));
  if (name === 'Codex') result.push(h('div', { class: 'qp-keys' }, `${s.source === 'template' ? 'Piper template' : 'Host Codex account'} · Codex usage API`));
  return result;
}
