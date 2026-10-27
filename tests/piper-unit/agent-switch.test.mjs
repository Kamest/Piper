// node --test: 9.140 - continue a project session's work with the other agent (Codex <-> Claude).
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const dist = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const convo = readFileSync(new URL('../../piper/web/src/convo.ts', import.meta.url), 'utf8');
const api = readFileSync(new URL('../../piper/web/src/api.ts', import.meta.url), 'utf8');
const fn = (src, name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n}\n', src.indexOf(`function ${name}(`)) + 2);

test('9.140: the target is the other agent, only for a project session; Codex only with its module', () => {
  const switchTarget = new Function(`${fn(dist, 'switchTarget')}; return switchTarget;`)();
  assert.equal(switchTarget({ kind: 'project', agent: 'codex' }, false), 'claude');
  assert.equal(switchTarget({ kind: 'project', agent: 'codex' }, true), 'claude');
  assert.equal(switchTarget({ kind: 'project', agent: 'claude' }, true), 'codex');
  assert.equal(switchTarget({ kind: 'project', agent: 'claude' }, false), null);
  assert.equal(switchTarget({ kind: 'project' }, true), 'codex', 'an older piperd without agent = Claude');
  assert.equal(switchTarget({ kind: 'project', agent: 'claude' }, false, {claude:true,codex:true}), 'codex');
  assert.equal(switchTarget({ kind: 'project', agent: 'codex' }, true, {claude:false,codex:true}), null);
  assert.equal(switchTarget({ kind: 'advisor', agent: 'codex' }, true), null);
  assert.equal(switchTarget({ kind: 'prodcheck', agent: 'claude' }, true), null);
});

test('9.140: the session menu - Continue with X… under the line before Continue / Stop, disabled without a conversation', () => {
  const m = fn(main, 'openRailMenu');
  const at = ['null,', 'switchTo ? { label: `Continue with ${agentName(switchTo)}…`', 'label: continueLabel(s)', "label: 'Stop…'"].map((k) => m.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.match(m, /disabled: !s\.claude_session && 'no saved conversation to hand over yet'/);
  assert.match(m, /run: \(\) => confirmSwitch\(s\)/);
  assert.match(main, /if \(switchTo\) sub\.push\(\{ label: `Continue with \$\{agentName\(switchTo\)\}…`/, '⌘K: the same action');
});

test('9.140: always confirmed; a 409 with hidden paths asks again (Hand over anyway), once', () => {
  const c = fn(main, 'confirmSwitch');
  assert.match(c, /api\.switchAgent\(s\.id, confirmHidden\)/);
  assert.match(c, /if \(paths && !confirmHidden\) confirmSwitch\(s, paths\);\s+else toast\(/);
  assert.match(c, /'Hand over anyway'/);
  assert.match(c, /Stops this \$\{agentName\(s\.agent \?\? 'claude'\)\} session \(its conversation stays resumable\)/);
  assert.match(api, /switchAgent: \(id: string, confirmHidden = false\) =>\s+call<[^>]+>\('POST', `\/api\/sessions\/\$\{encodeURIComponent\(id\)\}\/switch-agent`, confirmHidden \? \{ confirm_hidden: true \} : \{\}\)/);
});

test('9.140: the context panel names the source session and the worktree; the resume picker offers it for Piper sessions', () => {
  const p = fn(main, 'switchedProps');
  assert.match(p, /prop\('Continued from', `\$\{agentName\(f\.agent\)\} · \$\{src\?\.title \?\? f\.title \?\? f\.session\}`/);
  assert.match(p, /if \(f\.worktree\) out\.push\(prop\('Worktree'/);
  assert.match(main, /\.\.\.\(s\.switched_from \? switchedProps\(s\.switched_from\) : \[\]\),/);
  assert.match(convo, /const to = row && row\.source === 'piper' && !row\.closed \? deps\.switchTo\?\.\(row\.session\) \?\? null : null;/);
  assert.match(convo, /newConversation, handOver, fork, resume\)/);
  assert.match(main, /switchTo: \(id\) => \{ const s = session\(id\); const to = s\?\.claude_session \? switchTarget\(s, on\('codex'\), remoteAgents\(s.id\)\) : null;/);
});
