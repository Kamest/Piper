// W15: one "Start work" dialog for "what do I work on" (IntelliJ's New Project / Get from VCS): the kinds
// on the left (New session · From an issue · Resume conversation, ⌥1-3), the form on the right, one
// fixed size. Pure helpers here (node --test); main.ts startWork() builds the dialog.

import { baseProjectRef, rawProjectKey } from './environment.js';
import type { Agent, ProjectSessionDefaults, StartOpts } from './types.js';

export type WorkKind = 'new' | 'issue' | 'resume';

/** A dialog's unsaved drafts: switching/filtering projects never writes or loses another draft. */
export function startOptionsDrafts(saved: Record<string, ProjectSessionDefaults>, fallback: (keyof StartOpts)[]) {
  const drafts = new Map<string, { agent: Agent; opts: StartOpts; remember: boolean }>();
  return (project: string) => {
    const base = baseProjectRef(project);
    let draft = drafts.get(base);
    if (!draft) {
      const pref = saved[base];
      draft = { agent: pref?.agent ?? 'claude',
        opts: Object.fromEntries((pref?.opts ?? fallback).map((k) => [k, true])), remember: true };
      drafts.set(base, draft);
    }
    return draft;
  };
}

export interface WorkKindDef {
  id: WorkKind;
  label: string;
  sub: string;
  icon: string;
}

/** Only an untouched new draft may default to the sole installed agent. Never replace a user's choice. */
export function availableAgent(agent: Agent, available: Partial<Record<Agent, boolean>> | null | undefined, preferred: boolean, saved?: Agent): Agent {
  if (!preferred && saved) return saved;
  const other = agent === 'claude' ? 'codex' : 'claude';
  return !preferred && available?.[agent] === false && available?.[other] === true ? other : agent;
}

/** The work kinds; issue selection follows the GitLab/GitHub modules. */
export function workKinds(gitlab: boolean, github = false): WorkKindDef[] {
  const label = gitlab && github ? 'From an issue' : `From a ${gitlab ? 'GitLab' : 'GitHub'} issue`;
  return [
    { id: 'new', label: 'New session', sub: 'a project and a task', icon: '+' },
    ...(gitlab || github ? [{ id: 'issue' as const, label, sub: 'the task links the issue', icon: '#' }] : []),
    { id: 'resume', label: 'Resume conversation', sub: 'go on where it stopped', icon: '↺' },
  ];
}

/** ⌥1-3 (Alt off a Mac) → the index of the kind; by e.code, since ⌥digit types a character on a Mac */
export function kindKey(e: { altKey: boolean; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; code?: string }): number | null {
  if (!e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return null;
  const m = /^Digit([1-3])$/.exec(e.code ?? '');
  return m ? Number(m[1]) - 1 : null;
}

/** 9.54: the Options summary while an image rebuild is pending ('' = none) */
export function rebuildSummary(pending: boolean): string {
  return pending ? 'image rebuild at this start ⚠' : '';
}

/** F02: the folded Options line in the foot - every option that is on, or "sandbox default"; the agent has
 *  its own segment beside the project search. Any option on weakens the sandbox: a warning (amber, ⚠). */
export function optsSummary(picked: string[], labels: Record<string, string>): { text: string; risky: boolean } {
  const on = picked.map((k) => labels[k] ?? k);
  return { text: on.length ? on.join(' · ') : 'sandbox default', risky: on.length > 0 };
}

/** F02: an issue or merge request URL pasted as the task (Orca's paste-a-link). GitLab on any host
 *  (`…/-/issues/N`, `…/-/merge_requests/N`), GitHub (`/issues/N`, `/pull/N`) only with the github module;
 *  anything else - or a URL inside more text - stays plain task text. */
export interface ForgeRef { forge: 'gitlab' | 'github'; host: string; path: string; kind: 'issue' | 'mr'; iid: number }
export function parseForgeUrl(text: string, github: boolean): ForgeRef | null {
  const t = text.trim();
  const gl = /^https?:\/\/([^/\s]+)\/([^\s]+?)\/-\/(issues|merge_requests)\/(\d+)(?:[/?#]\S*)?$/.exec(t);
  if (gl) return { forge: 'gitlab', host: gl[1]!, path: gl[2]!, kind: gl[3] === 'issues' ? 'issue' : 'mr', iid: Number(gl[4]) };
  const gh = github ? /^https?:\/\/github\.com\/([^/\s]+\/[^/\s]+)\/(issues|pull)\/(\d+)(?:[/?#]\S*)?$/.exec(t) : null;
  if (gh) return { forge: 'github', host: 'github.com', path: gh[1]!, kind: gh[2] === 'issues' ? 'issue' : 'mr', iid: Number(gh[3]) };
  return null;
}

/** F02: the Piper project a pasted URL belongs to - its GitLab path (or `gh:owner/repo`) in projects.json,
 *  else the one project whose last name segment is the repository's; '' when none or ambiguous */
export function projectForRef(ref: ForgeRef, projects: { key: string; gitlab?: string[] }[]): string {
  const want = ref.forge === 'github' ? 'gh:' + ref.path : ref.path;
  const linked = projects.filter((p) => (p.gitlab ?? []).some((g) => g.toLowerCase() === want.toLowerCase()));
  if (linked.length === 1) return linked[0]!.key;
  const repo = (ref.path.split('/').pop() ?? '').toLowerCase();
  const named = projects.filter((p) => (rawProjectKey(p.key).split('/').pop() ?? '').toLowerCase() === repo);
  return named.length === 1 ? named[0]!.key : '';
}

/** F02: the chip under the task - "#412 title" (or "!7" for a merge request), the title once it is known */
export function refChip(ref: ForgeRef, title = ''): string {
  return `${ref.kind === 'mr' ? '!' : '#'}${ref.iid}${title ? ' ' + title : ''}`;
}

/** F02: the directory every project lives under (their longest common parent) - the list shows paths below it */
export function projectsRoot(paths: string[]): string {
  if (!paths.length) return '';
  const split = paths.map((p) => p.replace(/\/+$/, '').split('/'));
  const out: string[] = [];
  for (let i = 0; ; i++) {
    const seg = split[0]![i];
    // never the path itself: a single project still shows its parent
    if (seg === undefined || split.some((s) => s[i] !== seg || i >= s.length - 1)) break;
    out.push(seg);
  }
  return out.join('/');
}

/** Keep the full project key so equally named repositories remain distinguishable. */
export function projectRow(key: string, path: string, root: string): { name: string; dir: string } {
  const name = rawProjectKey(key);
  const rel = root && path.startsWith(root + '/') ? path.slice(root.length + 1) : path;
  const dir = rel.split('/').slice(0, -1).join('/');
  return { name, dir };
}


/** An unmapped checkout is a normal empty state; login, network and server failures remain errors. */
export function missingIssueTracker(status: number, message: string): boolean {
  return status === 400 && /no (?:GitLab project(?: and no GitHub repository)?|GitHub repository) is mapped/i.test(message);
}

/** 9.145: the default New worktree name - the task's first line (an attached issue's "#iid title"), folded to
 *  ascii, its first five words (the number counts), up to 40 characters; nothing to go by = session-MMDD-HHMM.
 *  `taken` (an existing worktree or feat/ branch) adds -2, -3 … */
export function worktreeSuggestion(source: string, taken: (name: string) => boolean, now = new Date()): string {
  const line = (source.split('\n').find((l) => l.trim()) ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const num = /^\s*[#!](\d+)\b/.exec(line)?.[1];
  const words = line.replace(/^\s*[#!]\d+\b/, '').split(/[^a-z0-9]+/).filter(Boolean);
  let base = [num, ...words].filter(Boolean).slice(0, 5).join('-');
  if (base.length > 40) {   // at a word boundary unless that leaves too little of one long word
    const at = base.slice(0, 41).lastIndexOf('-');
    base = (at >= 20 ? base.slice(0, at) : base.slice(0, 40)).replace(/-+$/, '');
  }
  const pad = (n: number) => String(n).padStart(2, '0');
  if (!base) base = `session-${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  let name = base;
  for (let i = 2; taken(name); i++) name = `${base}-${i}`;
  return name;
}

/** 9.146: what Start session is doing - the New worktree add, then the start itself. */
export type LaunchPhase = { kind: 'worktree'; name: string; from: string } | { kind: 'start' };

export function launchPhaseText(phase: LaunchPhase, seconds: number): string {
  const s = Math.floor(seconds);
  const took = s >= 2 ? ` ${s} s` : '';
  return phase.kind === 'worktree'
    ? `Creating worktree ${phase.name} from ${phase.from}…${took}`
    : `Starting the session…${took}`;
}
