// node --test: the one-time Security screen's pure parts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NOTICE_CHOICES, NOTICE_ID, NOTICE_ITEMS, noticeAction, noticeSelected, noticeWanted } from '../../piper/web/dist/securitynotice.js';

const n = (profile, kind = 'profile') => ({ kind, profile, on: 17, total: 64 });

test('the stored profile is preselected; an own mix gets Minimal as the starting point', () => {
  assert.equal(noticeSelected(n('hole')), 'hole');
  assert.equal(noticeSelected(n('recommended')), 'recommended');
  assert.equal(noticeSelected(n('custom', 'custom')), 'hole');
});

test('keeping the current profile only closes, another one applies', () => {
  assert.deepEqual(noticeAction(n('hole'), 'hole'), { label: 'Keep Minimal', apply: false });
  assert.deepEqual(noticeAction(n('hole'), 'recommended'), { label: 'Use Recommended', apply: true });
  assert.deepEqual(noticeAction(n('recommended'), 'guarded'), { label: 'Use Strict', apply: true });
});

test('opened once per page, never over the setup or another dialog', () => {
  assert.equal(noticeWanted(n('hole'), false, false), true);
  assert.equal(noticeWanted(n('hole'), true, false), false);
  assert.equal(noticeWanted(n('hole'), false, true), false);
  assert.equal(noticeWanted(null, false, false), false);
  assert.equal(noticeWanted(undefined, false, false), false);
});

test('the screen names the profiles Settings offers and the eight new Minimal items in six lines', () => {
  assert.deepEqual(NOTICE_CHOICES.map((c) => c[0]), ['hole', 'recommended', 'guarded']);
  assert.equal(NOTICE_ITEMS.length, 6);
  assert.equal(NOTICE_ID, 'security-2');
});
