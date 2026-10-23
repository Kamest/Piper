// Grid layout: up to 9 panes in a layout the user picks (Auto,
// Columns, Rows, 1 + rest, Big top, 2 × 2, 2 × 3, 3 × 3, 3 visible). A pane is a session, a host shell (`host-…`,
// W2), a Database pane (`db:1`..`db:3`, D1: `db:here` = the active session's project) or Files (`files`, D1) - all of
// it works on the id strings. D1/D2: the grid is the one multi-pane container - Beside (⌘O / ⌘D) and Split (⌘\) are
// panes next to the active session (addPane). A layout is a shape: groups of panes along the main
// axis (columns for 'col', rows for 'row'), each group split on its own across. The splits are fractions
// the user drags in the gaps between the panes; double-click centres one between its neighbours. Panes are
// absolutely positioned in % of the body, so a window resize needs no relayout; the terminals refit
// through their ResizeObserver. 9.45: the grid is the panes on screen, in the pick's order -
// no pins, a preset never hides a pane (more panes than it holds = Auto).

/** panes in the grid at most */
export const GRID_MAX = 9;
/** Database panes at most: each is a whole Database view instance */
export const GRID_DB_MAX = 3;
export const MIN_FR = 0.1;
const GAP = 10;   // px between panes (the splitter sits in it)

export type LayoutId = 'auto' | 'columns' | 'rows' | 'one-rest' | 'big-top' | '2x2' | '2x3' | '3x3' | '3-visible';

export interface Preset {
  id: LayoutId;
  label: string;
  /** panes it has a shape for; more panes = Auto (9.45: never hidden) */
  cap: number;
}

export const PRESETS: Preset[] = [
  { id: 'auto', label: 'Auto', cap: GRID_MAX },
  { id: 'columns', label: 'Columns', cap: GRID_MAX },
  { id: 'rows', label: 'Rows', cap: GRID_MAX },
  { id: 'one-rest', label: '1 + rest', cap: GRID_MAX },
  { id: 'big-top', label: 'Big top', cap: GRID_MAX },
  { id: '2x2', label: '2 × 2', cap: 4 },
  { id: '2x3', label: '2 × 3', cap: 6 },
  { id: '3x3', label: '3 × 3', cap: 9 },
  { id: '3-visible', label: '3 visible', cap: 3 },
];

export const presetOf = (id: string): Preset => PRESETS.find((p) => p.id === id) ?? PRESETS[0]!;   // 9.38's 'two' = Auto

/** 9.45: the layout n panes get - the picked one while they fit it, else Auto (a preset never hides a pane) */
export function layoutFor(id: string, n: number, width = Infinity, height = Infinity): LayoutId {
  const p = presetOf(id);
  if (n > p.cap) return 'auto';
  const s = shapeOf(p.id, n);
  const across = Math.max(1, ...s.groups);
  const cols = s.dir === 'col' ? s.groups.length : across;
  const rows = s.dir === 'row' ? s.groups.length : across;
  // A preset must leave room for the title and controls. Remember it, use Auto until the viewport fits it again.
  return (width - Math.max(0, cols - 1) * GAP) / Math.max(1, cols) < 220
    || (height - Math.max(0, rows - 1) * GAP) / Math.max(1, rows) < 140 ? 'auto' : p.id;
}

export interface LayoutChoice {
  id: LayoutId;
  on: boolean;
  disabled: boolean;
}

/** 9.135: the presets Beside ▾ offers for n panes - one card per distinct shape (with 2 panes Columns, 1 + rest, 2 × 3, …
 *  are all two columns: a click on another one changed nothing), named by the first preset of that shape and ticked when
 *  it holds the picked one; presets for fewer panes are left out; `fits` false = no room at this size (shown, disabled). Empty = nothing to choose. */
export function layoutChoices(n: number, eff: LayoutId, fits: (id: LayoutId) => boolean): LayoutChoice[] {
  if (n < 2) return [];
  const byShape = new Map<string, LayoutChoice>();
  for (const p of PRESETS) {
    if (n > p.cap) continue;
    const sh = shapeOf(p.id, n);   // one group of k = k groups of one the other way (2 × 2 for 2 panes is Rows)
    const k = shapeKey(sh.groups.length === 1 ? { dir: sh.dir === 'col' ? 'row' : 'col', groups: Array(sh.groups[0]!).fill(1) as number[] } : sh);
    const c = byShape.get(k) ?? byShape.set(k, { id: p.id, on: false, disabled: false }).get(k)!;   // the first preset names the shape
    if (p.id === eff) c.on = true;
  }
  const out = [...byShape.values()].map((c) => ({ ...c, disabled: !c.on && !fits(c.id) }));
  return out.filter((c) => !c.disabled).length < 2 ? [] : out;
}

/** groups along the main axis ('col' = columns side by side, 'row' = rows stacked), panes per group */
export interface Shape {
  dir: 'col' | 'row';
  groups: number[];
}

/** rows of `per` panes, the last one shorter */
const chunks = (n: number, per: number): number[] => Array.from({ length: Math.ceil(n / per) }, (_, i) => Math.min(per, n - i * per));

/** the shape of layout `id` for n panes (n already capped to its capacity) */
export function shapeOf(id: LayoutId, n: number): Shape {
  n = Math.max(0, Math.min(presetOf(id).cap, n));
  if (n <= 1) return { dir: 'col', groups: n ? [1] : [] };
  switch (id) {
    case 'columns':
    case '3-visible': return { dir: 'col', groups: Array(n).fill(1) as number[] };
    case 'rows': return { dir: 'row', groups: Array(n).fill(1) as number[] };
    case 'one-rest': return { dir: 'col', groups: [1, n - 1] };
    case 'big-top': return { dir: 'row', groups: [1, n - 1] };
    case '2x2': return { dir: 'col', groups: chunks(n, 2) };
    case '2x3':
    case '3x3': return { dir: 'row', groups: chunks(n, 3) };
    default: return { dir: 'col', groups: ({ 2: [1, 1], 3: [1, 2], 4: [2, 2], 5: [1, 2, 2], 6: [2, 2, 2], 7: [2, 2, 3], 8: [2, 3, 3] } as Record<number, number[]>)[n] ?? [3, 3, 3] };
  }
}

/** remembers the splits per shape: a 1 + 2 keeps its own, a 2 × 2 its own */
export const shapeKey = (s: Shape): string => `${s.dir}:${s.groups.join(',')}`;

/** the split positions, 0..1 ascending: between the groups (main) and inside every group (cross) */
export interface GridFr {
  main: number[];
  cross: number[][];
}

const even = (k: number): number[] => Array.from({ length: Math.max(0, k - 1) }, (_, i) => (i + 1) / k);

export function evenFr(s: Shape): GridFr {
  return { main: even(s.groups.length), cross: s.groups.map(even) };
}

/** splits that fit the shape: a missing / broken one (another shape, an old browser value) is even */
function ok(v: unknown, k: number): v is number[] {
  if (!Array.isArray(v) || v.length !== Math.max(0, k - 1)) return false;
  let at = 0;
  for (const x of v) {
    if (typeof x !== 'number' || !Number.isFinite(x) || x - at < MIN_FR - 1e-9) return false;
    at = x;
  }
  return 1 - at >= MIN_FR - 1e-9;
}

export function fitFr(s: Shape, fr: unknown): GridFr {
  const f = (fr ?? {}) as Partial<GridFr>;
  const e = evenFr(s);
  return {
    main: ok(f.main, s.groups.length) ? [...f.main] : e.main,
    cross: s.groups.map((k, i) => (Array.isArray(f.cross) && ok(f.cross[i], k) ? [...f.cross[i]!] : e.cross[i]!)),
  };
}

/** the splits from before W3 (one column split + one per column, key piper.gridfr) for the shapes they had */
export function legacyFr(s: Shape, old: unknown): GridFr | null {
  const o = (old ?? {}) as { col?: unknown; left?: unknown; right?: unknown };
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(1 - MIN_FR, Math.max(MIN_FR, v)) : 0.5);
  if (s.dir !== 'col' || typeof o.col !== 'number') return null;
  const key = s.groups.join(',');
  if (key === '1,1') return { main: [n(o.col)], cross: [[], []] };
  if (key === '1,2') return { main: [n(o.col)], cross: [[], [n(o.right)]] };
  if (key === '2,2') return { main: [n(o.col)], cross: [[n(o.left)], [n(o.right)]] };
  return null;
}

export interface Rect {
  left: string;
  top: string;
  width: string;
  height: string;
}

const pct = (f: number) => `${(f * 100).toFixed(3)}%`;
const bounds = (cuts: number[], i: number): [number, number] => [i === 0 ? 0 : cuts[i - 1]!, i === cuts.length ? 1 : cuts[i]!];

/** a box from main / cross ranges, half a gap taken from every side that touches a split */
function box(s: Shape, m: [number, number], c: [number, number]): Rect {
  const half = GAP / 2;
  const pm0 = m[0] > 0 ? half : 0;
  const pm1 = m[1] < 1 ? half : 0;
  const pc0 = c[0] > 0 ? half : 0;
  const pc1 = c[1] < 1 ? half : 0;
  const along = { start: `calc(${pct(m[0])} + ${pm0}px)`, size: `calc(${pct(m[1] - m[0])} - ${pm0 + pm1}px)` };
  const across = { start: `calc(${pct(c[0])} + ${pc0}px)`, size: `calc(${pct(c[1] - c[0])} - ${pc0 + pc1}px)` };
  const [x, y] = s.dir === 'col' ? [along, across] : [across, along];
  return { left: x.start, top: y.start, width: x.size, height: y.size };
}

/** CSS box of every pane, group by group (left to right / top to bottom), inside a group along the cross axis */
export function paneRects(s: Shape, fr: GridFr): Rect[] {
  return s.groups.flatMap((k, g) => Array.from({ length: k }, (_, i) => box(s, bounds(fr.main, g), bounds(fr.cross[g] ?? [], i))));
}

/** one splitter: group -1 = between the groups, else inside group `group`; `index` = which cut */
export interface Split {
  key: string;
  group: number;
  index: number;
  /** 'v' = a vertical bar dragged sideways */
  bar: 'v' | 'h';
  rect: Rect;
}

/** the splitters of a shape with their hit zones: 14 px centred in the 10 px gap */
export function splitBars(s: Shape, fr: GridFr): Split[] {
  const out: Split[] = [];
  const line = (at: number, span: [number, number], mainCut: boolean): Rect => {
    // a cut of the main axis is a line across it; a cut inside a group runs along the main axis over the group
    const pos = `calc(${pct(at)} - 7px)`;
    const half = GAP / 2;
    const p0 = span[0] > 0 ? half : 0;
    const p1 = span[1] < 1 ? half : 0;
    const from = `calc(${pct(span[0])} + ${p0}px)`;
    const len = `calc(${pct(span[1] - span[0])} - ${p0 + p1}px)`;
    const vertical = (s.dir === 'col') === mainCut;
    return vertical ? { left: pos, top: from, width: '14px', height: len } : { left: from, top: pos, width: len, height: '14px' };
  };
  fr.main.forEach((at, i) => out.push({ key: `m${i}`, group: -1, index: i, bar: s.dir === 'col' ? 'v' : 'h', rect: line(at, [0, 1], true) }));
  s.groups.forEach((_, g) => (fr.cross[g] ?? []).forEach((at, i) => out.push({
    key: `c${g}.${i}`, group: g, index: i, bar: s.dir === 'col' ? 'h' : 'v', rect: line(at, bounds(fr.main, g), false),
  })));
  return out;
}

/** move one cut to `v` (0..1 along its axis), kept MIN_FR from its neighbours */
export function dragSplit(fr: GridFr, group: number, index: number, v: number): GridFr {
  const cuts = group < 0 ? [...fr.main] : [...(fr.cross[group] ?? [])];
  if (index < 0 || index >= cuts.length) return fr;
  const lo = (index === 0 ? 0 : cuts[index - 1]!) + MIN_FR;
  const hi = (index === cuts.length - 1 ? 1 : cuts[index + 1]!) - MIN_FR;
  cuts[index] = Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : (lo + hi) / 2));
  return group < 0 ? { main: cuts, cross: fr.cross } : { main: fr.main, cross: fr.cross.map((c, i) => (i === group ? cuts : c)) };
}

/** double-click: the cut halfway between its neighbours */
export function resetSplit(fr: GridFr, group: number, index: number): GridFr {
  const cuts = group < 0 ? fr.main : fr.cross[group] ?? [];
  const lo = index === 0 ? 0 : cuts[index - 1]!;
  const hi = index === cuts.length - 1 ? 1 : cuts[index + 1]!;
  return dragSplit(fr, group, index, (lo + hi) / 2);
}

export type PaneKind = 'session' | 'host' | 'db' | 'files' | 'http';

/** D1: the Files pane (one - it lends the Files view's browser) and the Database pane that follows the active
 *  session's project (⌘D; not counted in GRID_DB_MAX - its connection list is the project's) */
export const FILES_PANE = 'files';
export const DB_HERE = 'db:here';
/** 9.42: the HTTP pane - the HTTP tab's panel lent to the grid like Files, on the active session's project */
export const HTTP_PANE = 'http';

/** W26: what a grid pane shows, by its id - a host shell is `host-<hex>` (hostterm.py), a Database pane `db:<n>` /
 *  `db:here`, Files `files`, HTTP `http` (9.42) */
export function paneKind(id: string): PaneKind {
  return id === FILES_PANE ? 'files' : id === HTTP_PANE ? 'http' : id.startsWith('host-') ? 'host' : /^db:([1-9]|here)$/.test(id) ? 'db' : 'session';
}

/** the panes next to the active session that follow its project: Files, HTTP (9.42), its Database */
const BESIDE = [FILES_PANE, HTTP_PANE, DB_HERE];

/** 9.37: a beside pair - one session with Files / HTTP / its project's Database next to it, nothing else. The Basic preset
 *  (no grid module) keeps this; the grid module gates only the grid of several sessions, host shells and db:1..3 */
export function besideOnly(ids: string[]): boolean {
  return ids.filter((x) => paneKind(x) === 'session').length <= 1 && ids.some((x) => BESIDE.includes(x)) && ids.every((x) => paneKind(x) === 'session' || BESIDE.includes(x));
}

/** the next Database pane id (the lowest free db:1..db:3), '' at GRID_DB_MAX */
export function nextDbPane(ids: string[]): string {
  for (let n = 1; n <= GRID_DB_MAX; n++) if (!ids.includes(`db:${n}`)) return `db:${n}`;
  return '';
}

/** the grid's panes in order (9.45: the pick is the whole state, no Automatic). A pick keeps its live sessions, its listed host shells (`hosts` null = the host module off:
 *  none) and its Database panes (`db` false = the module is off), once each, GRID_DB_MAX Database panes at most;
 *  9.42: the HTTP pane while the http module is on */
export function gridPanes(pick: string[], live: string[], hosts: string[] | null, db: boolean, http = true): string[] {
  const out: string[] = [];
  let dbs = 0;
  for (const id of pick) {
    if (out.includes(id)) continue;
    const k = paneKind(id);
    if (k === 'session' ? !live.includes(id) : k === 'host' ? !hosts?.includes(id) : k === 'files' ? false : k === 'http' ? !http
      : !db || (id !== DB_HERE && ++dbs > GRID_DB_MAX)) continue;
    out.push(id);
  }
  return out.slice(0, GRID_MAX);
}

/** a changed pick, remembered: the host shells of the old pick are kept while the policy is off (they come back
 *  with it - a shell closed for good is gone from piperd's list and drops out then); the same for Database panes and HTTP */
export function keepPick(next: string[], old: string[] | null, hostsOn: boolean, dbOn: boolean, httpOn = true): string[] {
  const keep = (old ?? []).filter((id) => !next.includes(id) && ((!hostsOn && paneKind(id) === 'host') || (!dbOn && paneKind(id) === 'db')
    || (!httpOn && paneKind(id) === 'http')));
  return [...next.slice(0, GRID_MAX), ...keep];
}

/** the parts of a session the split needs (main.ts Session) */
export interface PtyLike { id: string; pid: number | null; state: string }

/** the session's terminal is attached (main.ts draws a pane for it): a blocked / starting / ended one has none */
export const hasPty = (x: PtyLike): boolean => x.pid !== null && x.state !== 'blocked' && x.state !== 'preflight';

/** N10-A-2: why `partner` cannot show beside `active` ('' = it can). The split shows two terminals: with the active
 *  session blocked (or ended) main.ts drew its panel alone, so "Beside the active session" / ⌘\ did nothing visible
 *  and left a split that appeared later, when the session started */
export function splitBlock(active: PtyLike | undefined, partner: PtyLike | undefined): string {
  if (!active || !hasPty(active)) return 'the active session has no terminal (blocked, starting or ended)';
  if (partner && partner.id === active.id) return 'this is the active session';
  if (!partner || !hasPty(partner)) return 'this session has no terminal (blocked, starting or ended)';
  return '';
}

export interface PanePick {
  /** the grid's panes after the action */
  pick: string[];
  /** nothing changes; why */
  refuse?: string;
}

const FULL = `The grid has ${GRID_MAX} panes at most`;

/** 9.45: every add (⌘O, ⌘D, + Add pane, ⌘\, Side by side, Add to grid) - `pane` goes right after the focused pane
 *  (else after the active session's, else at the end), nothing is replaced; the active session (`active`, '' = it has
 *  no terminal) joins first when it is not in the grid. Already in = no change. */
export function addPane(members: string[], pane: string, focus: string, active: string): PanePick {
  if (members.includes(pane)) return { pick: members };
  const pick = [...members];
  if (active && active !== pane && !pick.includes(active)) pick.push(active);
  if (pick.length >= GRID_MAX) return { pick: members, refuse: FULL };
  const at = [focus, active].map((x) => (x ? pick.indexOf(x) : -1)).find((i) => i >= 0) ?? -1;
  pick.splice(at < 0 ? pick.length : at + 1, 0, pane);
  return { pick };
}

/** 9.45: ✕ - the pane leaves the grid; `next` = its neighbour (the one after, else before) for the focus */
export function removePane(members: string[], pane: string): { pick: string[]; next: string } {
  const i = members.indexOf(pane);
  if (i < 0) return { pick: members, next: '' };
  const pick = members.filter((x) => x !== pane);
  return { pick, next: pick[Math.min(i, pick.length - 1)] ?? '' };
}

/** 9.45: drag a pane's title onto another / ◀ ▶ - the two swap places */
export function swapPanes(members: string[], a: string, b: string): string[] {
  const i = members.indexOf(a);
  const j = members.indexOf(b);
  if (i < 0 || j < 0 || i === j) return members;
  const out = [...members];
  [out[i], out[j]] = [out[j]!, out[i]!];
  return out;
}

/** 9.45: a live session picked in the queue while the grid shows and it is not in it: it takes the focused session
 *  pane (else the active session's, else the first session pane; none = it joins at the start) */
export function replaceFocused(members: string[], to: string, focus: string, active: string): string[] {
  if (members.includes(to)) return members;
  const sess = (x: string) => !!x && paneKind(x) === 'session' && members.includes(x);
  const at = sess(focus) ? focus : sess(active) ? active : members.find((x) => paneKind(x) === 'session') ?? '';
  return at ? members.map((x) => (x === at ? to : x)) : [to, ...members].slice(0, GRID_MAX);
}

/** 9.45: `piper.grid` from before 9.45 had null = Automatic (the live sessions); it becomes them once when the grid
 *  was on screen, else an empty grid */
export function migratePick(stored: string[] | null, wasGrid: boolean, live: string[]): string[] {
  return stored ?? (wasGrid ? live.slice(0, GRID_MAX) : []);
}
