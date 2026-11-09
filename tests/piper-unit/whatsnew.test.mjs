// node --test: the one-time What's new screen's pure parts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CHANGELOG_URL, DOWNLOAD_URL, NOTICE_ID, PHONE_URL, REPO_URL, WHATS_NEW_ITEMS, WHATS_NEW_MORE, whatsNewWanted } from '../../piper/web/dist/whatsnew.js';

const n = { id: 'whats-new-1', since: '2026-10-13' };

test('opened once per page, never over a dialog, never after another one-time screen on the same page load', () => {
  assert.equal(whatsNewWanted(n, false, false, false), true);
  assert.equal(whatsNewWanted(n, true, false, false), false, 'already shown on this page');
  assert.equal(whatsNewWanted(n, false, true, false), false, 'setup / Security / restore opened on this page: next open');
  assert.equal(whatsNewWanted(n, false, false, true), false, 'the setup view or a dialog is up');
  assert.equal(whatsNewWanted(null, false, false, false), false);
  assert.equal(whatsNewWanted(undefined, false, false, false), false);
});

test('the id is the one piperd offers and stores as seen', () => {
  assert.equal(NOTICE_ID, 'whats-new-1');
  assert.match(CHANGELOG_URL, /^https:\/\/github\.com\/Kamest\/Piper\//);
});

test('the hero and the cards lead to the downloads, the repository and the phone client', () => {
  assert.equal(DOWNLOAD_URL, 'https://kamest.github.io/Piper/#desktop');
  assert.equal(REPO_URL, 'https://github.com/Kamest/Piper');
  assert.deepEqual(WHATS_NEW_ITEMS.filter((i) => i.link).map((i) => [i.title, i.link.href]), 
    [['Desktop app', DOWNLOAD_URL], ['Piper on your phone', PHONE_URL]]);
  assert.equal(PHONE_URL, 'https://piper.kamest.dev');
});

test('eight main highlights keep direct server sessions untagged and the phone beta', () => {
  assert.equal(WHATS_NEW_ITEMS.length, 8);
  assert.equal(new Set(WHATS_NEW_ITEMS.map((i) => i.title)).size, 8);
  for (const it of WHATS_NEW_ITEMS) {
    assert.ok(it.icon.length > 0 && it.icon.every((d) => typeof d === 'string' && d.length > 0), it.title);
    assert.ok(it.text.length > 20 && it.text.length < 200, it.title);
  }
  assert.deepEqual(WHATS_NEW_ITEMS.filter((i) => i.tag).map((i) => [i.title, i.tag]),
    [['Piper on your phone', 'BETA']]);
  const server = WHATS_NEW_ITEMS.find((i) => i.title === 'Your own Piper server');
  assert.match(server.text, /new sessions/i);
  assert.doesNotMatch(server.text, /Move to server|already running/i);
  const workspaces = WHATS_NEW_ITEMS.find((i) => i.title === 'Workspaces');
  assert.match(workspaces.text, /desktop app.*window bar.*without reloading open terminals/);
});

test('only moving a running session is experimental and stays in Also new', () => {
  assert.deepEqual(WHATS_NEW_MORE.map((i) => i.title),
    ['Move to server', 'A calmer look', 'New session', 'Phone backup', 'Later', 'Wrapped links', 'Stack overview', 'Git renames', 'Extensions']);
  assert.equal(new Set(WHATS_NEW_MORE.map((i) => i.title)).size, 9);
  assert.deepEqual(WHATS_NEW_MORE.filter((i) => i.tag).map((i) => [i.title, i.tag]),
    [['Move to server', 'EXPERIMENTAL']]);
  assert.match(WHATS_NEW_MORE[0].text, /running session/i);
  for (const it of WHATS_NEW_MORE) assert.ok(it.text.length > 20 && it.text.length < 200, it.title);
  assert.ok(WHATS_NEW_ITEMS.every((i) => !/Clone a repository|one backup computer/.test(i.text)));
  assert.match(WHATS_NEW_MORE.find((i) => i.title === 'New session').text, /Clone a repository/);
  assert.match(WHATS_NEW_MORE.find((i) => i.title === 'Phone backup').text, /phone beta/);
  assert.match(WHATS_NEW_MORE.find((i) => i.title === 'Stack overview').text, /Optional feature/);
});
