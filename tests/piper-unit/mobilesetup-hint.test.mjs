// node --test: Settings › Mobile access says how to start the bridge, not just that it is missing.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../../piper/web/src/mobilesettings.ts', import.meta.url), 'utf8');
const dist = readFileSync(new URL('../../piper/web/dist/mobilesettings.js', import.meta.url), 'utf8');

test('mobile access not set up / bridge stopped names the command that fixes it', () => {
  for (const text of [src, dist]) {
    assert.ok(text.includes('Start the mobile bridge on this computer: task piper:mobile -- install, then task piper:mobile -- serve.'));
    assert.ok(text.includes('can’t connect until it’s back: task piper:mobile -- serve'));
    assert.ok(!text.includes('has no relay settings'));
  }
});

test('review 2026-10-31 (G4): every pairing phase re-render keeps the keyboard inside the dialog', async () => {
  const { readFileSync } = await import('node:fs');
  const dist = readFileSync(new URL('../../piper/web/dist/mobilesettings.js', import.meta.url), 'utf8');
  assert.match(dist, /if \(phase !== 'approve' && !root\.contains\(document\.activeElement\)\)\s*root\.querySelector\('\.row button\.primary, \.row button'\)\?\.focus\(\{ preventScroll: true \}\);/);
});
