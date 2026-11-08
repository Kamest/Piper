import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { projectFileTargets, relativeFile } from '../../piper/web/dist/references.js';
import { reference, refHost, scoped, rawProjectKey } from '../../piper/web/dist/environment.js';
import { IMAGE_RE } from '../../piper/web/dist/files.js';

const source = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('async function openReference('), source.indexOf('async function openOutsideFile('));

function fixture({ projects = [], host = '', entry = null } = {}) {
  const opened = [], choices = [], targets = [], shown = [], images = [], loads = [];
  const session = { id: scoped(host, 'session', 'source'), project: scoped(host, 'project', 'acme/app'), path: '/projects/app', kind: 'project' };
  const deps = {
    state: { projects }, sshOnly: () => false, reference, refHost, relativeFile, projectFileTargets,
    api: { list: async () => ({ data: { entries: entry ? [entry] : [] } }) },
    ApiError: class extends Error {},
    openOutsideFile: async (v, path, line) => opened.push({ host: refHost(v.id), path, line }),
    referenceSession: () => {}, openTermFile: (_v, path) => shown.push(path), on: () => false,
    linkChoices: (title, items) => choices.push({ title, items }), linkTask: task => task,
    call: async (...args) => { targets.push(args); return { data: { kind: 'missing' } }; },
    fsIndex: async () => ({ data: { files: [] } }), fileMatches: () => [], toast: () => {},
    IMAGE_RE, rawProjectKey, modalOver: () => {}, closeModal: () => {},
    openImageView: (_deps, source) => images.push(source),
    rawImage: async (project, path) => { loads.push({ project, path }); return 'blob:image'; },
  };
  const open = new Function(...Object.keys(deps), code + ';return openReference;')(...Object.values(deps));
  return { session, opened, choices, targets, shown, images, loads, open: path => open(session, { kind: 'file', path, line: 7 }) };
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
