import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { projectFileTargets, relativeFile } from '../../piper/web/dist/references.js';
import { reference, refHost, scoped, rawProjectKey, baseProjectRef } from '../../piper/web/dist/environment.js';
import { IMAGE_RE } from '../../piper/web/dist/files.js';

const source = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('async function openReference('), source.indexOf('async function openOutsideFile('));

class ApiError extends Error {}

function fixture({ projects = [], host = '', entry = null, found = { matches: [], truncated: false }, findError = null, worktree = '', existing = [] } = {}) {
  const opened = [], choices = [], targets = [], shown = [], images = [], loads = [], toasts = [], finds = [];
  const session = { id: scoped(host, 'session', 'source'), project: scoped(host, 'project', 'acme/app' + (worktree ? '@' + worktree : '')), path: '/projects/app', kind: 'project' };
  const deps = {
    state: { projects }, sshOnly: () => false, reference, refHost, relativeFile, projectFileTargets,
    api: { list: async () => ({ data: { entries: entry ? [entry] : [] } }) },
    ApiError,
    openOutsideFile: async (v, path, line) => opened.push({ host: refHost(v.id), path, line }),
    referenceSession: () => {}, openTermFile: (_v, path) => shown.push(path), on: () => false,
    linkChoices: (title, items) => choices.push({ title, items }), linkTask: task => task,
    call: async (...args) => { targets.push(args); const path = decodeURIComponent(args[1].split('path=')[1]); return { data: { kind: existing.includes(path) ? 'file' : 'missing' } }; },
    fsIndex: async () => ({ data: { files: ['old/a.ts'] } }), fileMatches: (files) => files, toast: message => toasts.push(message),
    fsFind: async (_project, path) => { finds.push(path); if (findError) throw findError; return { data: found }; }, baseProjectRef,
    IMAGE_RE, rawProjectKey, modalOver: () => {}, closeModal: () => {},
    openImageView: (_deps, source) => images.push(source),
    rawImage: async (project, path) => { loads.push({ project, path }); return 'blob:image'; },
  };
  const open = new Function(...Object.keys(deps), code + ';return openReference;')(...Object.values(deps));
  return { session, opened, choices, targets, shown, images, loads, toasts, finds, open: path => open(session, { kind: 'file', path, line: 7 }) };
}

const project = (key, path, exists = true) => ({ key, path, exists });

test('a sibling folder opens on its emitting host after the exact checkout path misses', async () => {
  const f = fixture({ projects: [project('acme/helper-kit', '/projects/helper-kit')] });
  await f.open('helper-kit/.gitlab-ci.yml');
  assert.deepEqual(f.opened, [{ host: '', path: '/projects/helper-kit/.gitlab-ci.yml', line: 7 }]);
});

test('an exact file in the current checkout takes priority over a registered sibling', async () => {
  const f = fixture({ projects: [project('acme/helper-kit', '/projects/helper-kit')], entry: { name: '.gitlab-ci.yml', type: 'file' } });
  await f.open('helper-kit/.gitlab-ci.yml');
  assert.deepEqual(f.shown, ['helper-kit/.gitlab-ci.yml']);
  assert.deepEqual(f.opened, []);
  assert.deepEqual(f.targets, []);
});

test('an image loads from its emitting project and host without opening another source view', async () => {
  for (const host of ['', 'server']) {
    const f = fixture({ host, entry: { name: 'draft.png', type: 'file' } });
    await f.open('draft.png');
    assert.equal(f.images.length, 1);
    assert.equal(f.images[0].context, 'acme/app');
    assert.deepEqual(f.loads, [], 'opening the viewer defers its original read to acquire()');
    assert.equal(await f.images[0].load(), 'blob:image');
    assert.deepEqual(f.loads, [{ project: scoped(host, 'project', 'acme/app'), path: 'draft.png' }]);
    assert.deepEqual(f.shown, []);
    assert.deepEqual(f.opened, []);
  }
});

test('ambiguous folder prefixes await an explicit project choice', async () => {
  const f = fixture({ projects: [project('acme/helper-kit', '/projects/helper-kit'), project('other/helper-kit', '/other/helper-kit')] });
  await f.open('helper-kit/.gitlab-ci.yml');
  assert.deepEqual(f.opened, []);
  assert.equal(f.choices.length, 1);
  assert.deepEqual(f.choices[0].items.map(x => x.label), ['acme/helper-kit', 'other/helper-kit']);
  await f.choices[0].items[1].run();
  assert.equal(f.opened[0].path, '/other/helper-kit/.gitlab-ci.yml');
});

test('remote references only consider existing projects from the same host', async () => {
  const f = fixture({ host: 'server', projects: [
    project('acme/helper-kit', '/notebook/helper-kit'),
    project(scoped('other', 'project', 'acme/helper-kit'), '/other/helper-kit'),
    project(scoped('server', 'project', 'acme/helper-kit'), '/server/helper-kit'),
    project(scoped('server', 'project', 'archived/helper-kit'), '/archive/helper-kit', false),
  ] });
  await f.open('helper-kit/.gitlab-ci.yml');
  assert.deepEqual(f.opened, [{ host: 'server', path: '/server/helper-kit/.gitlab-ci.yml', line: 7 }]);
});

test('a remote reference never falls back to a notebook sibling', async () => {
  const f = fixture({ host: 'server', projects: [project('acme/helper-kit', '/notebook/helper-kit')] });
  await f.open('helper-kit/.gitlab-ci.yml');
  assert.deepEqual(f.opened, []);
  assert.equal(f.targets[0][5], 'server');
});

test('a terminal link searches the checkout on the server and opens the one versioned match', async () => {
  const f = fixture({ found: { truncated: false, matches: [
    { path: 'development/copy/src/feed.ts', type: 'file', tracked: false },
    { path: 'src/feed.ts', type: 'file', tracked: true }] } });
  await f.open('feed.ts');
  assert.deepEqual(f.finds, ['feed.ts']);
  assert.deepEqual(f.shown, ['src/feed.ts']);
  assert.deepEqual(f.choices, []);
});

test('several versioned matches ask, versioned first; nothing within the time limit says so', async () => {
  const f = fixture({ found: { truncated: false, matches: [
    { path: 'development/a/README.md', type: 'file', tracked: false },
    { path: 'a/README.md', type: 'file', tracked: true }, { path: 'b/README.md', type: 'file', tracked: true }] } });
  await f.open('README.md');
  assert.deepEqual(f.choices[0].items.map(x => x.label), ['a/README.md', 'b/README.md', 'development/a/README.md']);
  const g = fixture({ found: { matches: [], truncated: true } });
  await g.open('missing/x.md');
  assert.deepEqual(g.toasts, ['Not found within the search time limit: missing/x.md']);
});

test('a home path opens as the host file and an older server falls back to its listing', async () => {
  const f = fixture();
  await f.open('~/.piper/limits.json');
  assert.deepEqual(f.opened, [{ host: '', path: '~/.piper/limits.json', line: 7 }]);
  assert.deepEqual(f.finds, []);
  const g = fixture({ findError: new ApiError("unknown fs operation 'find'") });
  await g.open('a.ts');
  assert.deepEqual(g.shown, ['old/a.ts']);
});

test('a worktree session opens a file that only exists in the main checkout (unversioned development/)', async () => {
  const f = fixture({ worktree: 'fix', projects: [project('acme/app', '/projects/main')], existing: ['/projects/main/development/plan.md'] });
  await f.open('development/plan.md');
  assert.deepEqual(f.opened, [{ host: '', path: '/projects/main/development/plan.md', line: 7 }]);
  const g = fixture({ projects: [project('acme/app', '/projects/main')], existing: ['/projects/main/development/plan.md'] });
  await g.open('development/plan.md');
  assert.deepEqual(g.opened, [], 'the main checkout itself has no other checkout to fall back to');
});
