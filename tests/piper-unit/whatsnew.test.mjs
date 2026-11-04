// node --test: the one-time What's new screen's pure parts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CHANGELOG_URL, DOWNLOAD_URL, NOTICE_ID, WHATS_NEW_ITEMS, whatsNewWanted } from '../../piper/web/dist/whatsnew.js';

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

test('the footer leads to the desktop app downloads on the project page', () => {
  assert.equal(DOWNLOAD_URL, 'https://kamest.github.io/Piper/#desktop');
});

test('eight distinct items with an icon and a text; only the phone is marked beta', () => {
  assert.equal(WHATS_NEW_ITEMS.length, 8);
  assert.equal(new Set(WHATS_NEW_ITEMS.map((i) => i.title)).size, 8);
  for (const it of WHATS_NEW_ITEMS) {
    assert.ok(it.icon.length > 0 && it.icon.every((d) => typeof d === 'string' && d.length > 0), it.title);
    assert.ok(it.text.length > 20 && it.text.length < 140, it.title);
  }
  assert.deepEqual(WHATS_NEW_ITEMS.filter((i) => i.tag).map((i) => [i.title, i.tag]), [['Piper on your phone', 'BETA']]);
});
