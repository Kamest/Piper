// node --test: F27 Push & MR - one dialog: the commits in sight, the diff folded, the MR rides along.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { canOfferMrWithPush, canOfferMr } from '../../piper/web/dist/gitpanel.js';
import { pushGoLabel } from '../../piper/web/dist/gitwindow.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const win = src('gitwindow.ts');
const git = src('gitpanel.ts');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const fn = (s, name) => s.slice(s.indexOf(`function ${name}(`), s.indexOf('\n  }\n', s.indexOf(`function ${name}(`)) + 4);

test('F27: the MR is offered with the push - a named branch, not the main line, no MR yet; unpushed commits are fine', () => {
  const ci = { mr: null, branch: 'feat/x' };
  assert.equal(canOfferMrWithPush({ branch: 'feat/x' }, ci), true);
  assert.equal(canOfferMrWithPush({ branch: 'feat/x', ahead: 3, upstream: '' }, ci), true, 'before the first push');
  assert.equal(canOfferMr({ branch: 'feat/x', ahead: 3, upstream: '' }, ci), false, 'the Create MR… chip still waits for the push');
  assert.equal(canOfferMrWithPush({ branch: 'main' }, { ...ci, branch: 'main' }), false);
  assert.equal(canOfferMrWithPush({ branch: 'develop' }, { ...ci, branch: 'develop' }), false);
  assert.equal(canOfferMrWithPush({ branch: 'feat/x' }, { ...ci, mr: { iid: 3 } }), false, 'an MR exists');
  assert.equal(canOfferMrWithPush({ branch: 'feat/x' }, null), false, 'CI unknown (no GitLab / GitHub)');
  assert.equal(canOfferMrWithPush({ branch: '' }, ci), false, 'a detached HEAD');
  assert.equal(canOfferMrWithPush({ branch: 'feat/x' }, { ...ci, branch: 'other' }), false, 'stale CI of another branch');
});

test('F27: one button - "Push & open MR" while the MR is ticked, else "Push N commits"', () => {
  const p = { commits: [{}, {}, {}], more: false };
  assert.equal(pushGoLabel(p, true, false), 'Push & open MR');
  assert.equal(pushGoLabel(p, true, true), 'Push & open PR');
  assert.equal(pushGoLabel(p, false, false), 'Push 3 commits');
  assert.equal(pushGoLabel({ commits: [{}], more: false }, false, false), 'Push 1 commit');
  assert.equal(pushGoLabel({ commits: [], more: false }, false, false), 'Push', 'a new branch without new commits');
});

test('F27: the dialog - title, checks, the commits, files + diff folded behind "show diff", the MR section, git command, Cancel, ⌘⏎', () => {
  assert.match(win, /checks, brief, detail, mrBox\?\.el \?\? null,/, 'the board\'s order');
  assert.match(win, /const detail = h\('div', \{ class: 'pd-detail hidden' \},/, 'files and diff folded by default');
  assert.match(win, /`\$\{p\.files\.length\} \$\{p\.files\.length === 1 \? 'file' : 'files'\} · \$\{stat\(add, del\)\}`/);
  assert.match(win, /if \(e\.key === 'Enter' && \(e\.metaKey \|\| e\.ctrlKey\) && !go\.disabled\)/);
  assert.match(win, /`Then open a \$\{mr\.gh \? 'pull' : 'merge'\} request into`/);
  assert.match(win, /onclick: \(\) => void generate\(\) \}, \.\.\.generateLabel\(\)\)/, '9.60 Generate moved into the section (9.112: sparkles icon)');
  assert.match(win, /go\.disabled = pushButton\(p\)\.disabled \|\| \(on\.checked && \(!name\.value\.trim\(\) \|\| !into\.value\.trim\(\)\)\);/,
    'a block check still disables it; a ticked MR needs a title and a target');
  assert.match(win, /values: \(\) => on\.checked && base \? \{/, 'unticked = push only');
});

test('F27: no second confirmation - the MR is created after a successful push; the Create MR… form creates it directly', () => {
  assert.doesNotMatch(git, /confirmMr|Create the MR in GitLab\?|Continue…/);
  const push = fn(git, 'confirmPush');
  assert.match(push, /if \(open\) void createMr\(id, project, open, gh\);   \/\/ only after the push went through/);
  assert.ok(push.indexOf('api.push(id).then(') < push.indexOf('createMr('), 'never before the push');
  assert.match(push, /canOfferMrWithPush\(of\(id\)\.status, ci\)/);
  assert.match(git, /deps\.closeModal\(\);\n\s+void createMr\(id, project, x\);/);
});

test('F27: CSS - a compact dialog that grows only with the diff open', () => {
  const block = css.slice(css.indexOf('/* ==== 9.88 F27'), css.indexOf('/* ==== end F27 ==== */'));
  assert.match(block, /\.modal:has\(\.pd\) \{ width: min\(640px, calc\(100vw - 32px\)\); height: auto;/);
  assert.match(block, /\.modal:has\(\.pd\.pd-open\) \{ width: min\(1040px/);
});
