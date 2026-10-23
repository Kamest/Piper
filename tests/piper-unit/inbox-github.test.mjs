// node --test: W23 GitHub in the inbox GUI - the forge helpers (web/src/inbox.ts) and the task mirror (text.ts)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { forgeName, inboxName, isGh, ref } from '../../piper/web/dist/inbox.js';
import { issueTask, taskFor } from '../../piper/web/dist/text.js';

test('inboxName: GitLab alone, GitHub alone (GitLab off), Inbox with both', () => {
  assert.equal(inboxName(null), 'GitLab');
  assert.equal(inboxName({ status: 'ok' }), 'GitLab');
  assert.equal(inboxName({ status: 'ok', github: null }), 'GitLab');           // GitHub module off
  assert.equal(inboxName({ status: 'ok', github: 'no-token' }), 'Inbox');      // both on (GitHub not logged in yet)
  assert.equal(inboxName({ status: 'off', github: 'ok' }), 'GitHub');
  assert.equal(inboxName({ status: 'ok', github: { status: 'ok' } }), 'Inbox'); // the full /api/inbox shape
});

test('ref: !n only for a GitLab MR, a GitHub PR is #n', () => {
  assert.equal(ref({ type: 'mr', iid: 7, gl: 'grp/app' }), '!7');
  assert.equal(ref({ type: 'mr', iid: 7 }), '!7');
  assert.equal(ref({ type: 'mr', iid: 7, gl: 'gh:kamest/app' }), '#7');
  assert.equal(ref({ type: 'issue', iid: 3, gl: 'grp/app' }), '#3');
});

test('isGh / forgeName follow the gh: prefix', () => {
  assert.equal(isGh({ gl: 'gh:kamest/app' }), true);
  assert.equal(isGh({ gl: 'grp/gh/app' }), false);
  assert.equal(forgeName({ gl: 'gh:ghe.example.com/team/app' }), 'GitHub');
  assert.equal(forgeName({ gl: 'grp/app' }), 'GitLab');
});

test('taskFor mirror of task_for for GitHub: github_pr / github_issue, repo + number, github- drafts', () => {
  const pr = { type: 'mr', iid: 7, title: 'Fix', web_url: 'https://github.com/kamest/app/pull/7', gl: 'gh:kamest/app' };
  const work = taskFor(pr, 'work', 55).split('\n');
  assert.deepEqual(work.slice(0, 3), ['GitHub pull request #7: Fix', 'https://github.com/kamest/app/pull/7', '']);
  assert.equal(work[3], 'Handle comment note_55 (and anything newer) - that is the task now. Read it with the tool github_pr (repo "kamest/app", number 7). The description and older comments are context only: do not re-analyse the whole pull request, look back only where the comment needs it.');
  assert.match(work[5], /^Save a GitHub comment draft to development\/github-pr-7-comment\.md /);
  const an = taskFor({ ...pr, type: 'issue', iid: 3 }, 'analyze').split('\n');
  assert.equal(an[3], 'Read the task including all comments with the tool github_issue (repo "kamest/app", number 3) - the comments are part of the task.');
  assert.match(an[4], /development\/github-3-analysis\.md/);
  assert.doesNotMatch(taskFor(pr, 'work'), /gitlab/i);
});

test('issueTask for a gh: project (Start work from a GitHub issue): github_issue, repo + number, no GitLab words', () => {
  const i = { iid: 4, title: 'Price list', state: 'open', web_url: 'https://github.com/Kamest/App/issues/4' };
  const t = issueTask(i, 'gh:kamest/app').split('\n');
  assert.deepEqual(t.slice(0, 3), ['GitHub issue #4: Price list', 'https://github.com/Kamest/App/issues/4', '']);
  assert.equal(t[3], 'Read the task including all comments with the tool github_issue (repo "kamest/app", number 4) - the comments are part of the task.');
  assert.match(issueTask(i, 'gh:kamest/app', 9), /Handle comment note_9 .* github_issue .* the whole issue,/);
  assert.doesNotMatch(issueTask(i, 'gh:kamest/app', 9), /gitlab/i);
});

test('the session panel names its GitHub repositories (Network) while the module is on', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /on\('github'\) && \(s\.kind === 'project' \|\| s\.github\?\.length\) \? prop\('GitHub', /);
});
