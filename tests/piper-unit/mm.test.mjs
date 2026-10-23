// node --test: the Mattermost status dialog's text, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { MM_AFTER, MM_DETAIL, mmAdvancedSummary, mmStatusLine, statusLines } from '../../piper/web/dist/mm.js';

const base = {
  configured: false, mode: 'webhook', replies: false, detail: 'command', quiet: false, visible: true,
  last_error: '', last_ok: 0, webhook_file: '/h/.piper/mattermost-webhook', config_file: '/h/.piper/mattermost.json',
  note: 'Incoming webhook: notifications only.',
};

test('not configured: says which file and which command', () => {
  const l = statusLines(base);
  assert.match(l[0], /Not set up: \/h\/\.piper\/mattermost-webhook is missing/);
  assert.match(l[1], /piper\.sh mattermost-webhook/);
  assert.equal(l.at(-1), base.note);
});

test('configured: detail, quiet hours and the last error', () => {
  const l = statusLines({ ...base, configured: true, detail: 'none', quiet: true, last_error: 'webhook: HTTP 404' });
  assert.match(l[0], /incoming webhook/);
  assert.ok(l.some((x) => /Details in the message: none/.test(x)));
  assert.ok(l.some((x) => /Quiet hours/.test(x)));
  assert.ok(l.some((x) => /HTTP 404/.test(x)));
});

test('N10-D-13: not configured points at Settings → Mattermost (it has the Webhook URL field), the script stays the fallback', () => {
  const l = statusLines(base);
  assert.match(l[1], /^Save the webhook URL in Settings → Mattermost/);
});

test('F22 the status line: loading, not set up, the last error, connected (with the draft channel), quiet hours', () => {
  assert.deepEqual(mmStatusLine(null), { tone: 'mut', text: 'Checking the connection…' });
  assert.deepEqual(mmStatusLine(base), { tone: 'mut', text: 'Not set up - paste the webhook URL below' });
  const on = { ...base, configured: true };
  assert.deepEqual(mmStatusLine({ ...on, last_error: 'webhook: HTTP 404', last_ok: 1 }), { tone: 'bad', text: 'Last message failed: webhook: HTTP 404' });
  assert.deepEqual(mmStatusLine(on, 'piper-alerts'), { tone: 'ok', text: 'Webhook set · no message sent yet to ~piper-alerts' });
  const now = Date.now() / 1000;
  const ok = mmStatusLine({ ...on, last_ok: now, quiet: true }, 'piper-alerts');
  assert.equal(ok.tone, 'ok');
  assert.match(ok.text, /^Connected · last message \S.* to ~piper-alerts · quiet hours now$/);   // today: the time of day
  assert.match(mmStatusLine({ ...on, last_ok: now - 3 * 86400 }).text, /^Connected · last message 3 d ago$/);
});

test('F22 human values: 1 / 3 / 10 min, three short message names, the Advanced line names the rate limit', () => {
  assert.deepEqual(MM_AFTER.map(([s]) => s), [60, 180, 600]);
  assert.deepEqual(MM_DETAIL, { none: 'Title only', command: '+ command', full: 'Full answer' });
  assert.equal(mmAdvancedSummary(900), 'Advanced — rate limit (1 per session / 15 min), channel, sender name');
  assert.equal(mmAdvancedSummary(90), 'Advanced — rate limit (1 per session / 90 s), channel, sender name');
  assert.equal(mmAdvancedSummary(0), 'Advanced — no rate limit, channel, sender name');
});

test('F22 Settings › Mattermost: status + Send test on top, segments, one quiet-hours row, Advanced, the F19 Save bar; presence kept', () => {
  const st = readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');
  const mm = readFileSync(new URL('../../piper/web/src/mm.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(st, /const mmView = s\.id === 'mattermost' && fields === s\.fields;/);   // a search shows the plain rows
  assert.match(st, /: mmView \? \[mattermostEl\(s, q\)\]/);
  assert.match(st, /onclick: \(\) => void action\(s, 'mm-test'\) \}, 'Send test'\)/);
  assert.match(st, /\.filter\(\(a\) => !\(mmView && a\.id === 'mm-test'\)\)/);   // not twice: the bar has no test button
  assert.match(st, /row\('Message', seg\('detail'/);
  assert.match(st, /row\('Notify after', seg\('after_s', MM_AFTER\.map/);
  assert.match(st, /row\('Quiet hours', control\(s, fld\('quiet_from'\)!\), h\('span', \{ class: 'mut' \}, '–'\), control\(s, fld\('quiet_to'\)!\),/);
  assert.match(st, /const advanced = \['min_gap_s', 'after_s', 'channel', 'username'\]/);
  assert.match(st, /onclick: \(\) => \{ setVal\(sid, key, val\); draw\(\); \}/);   // a segment is a draft: Save in the sticky bar
  assert.match(st, /await req\('GET', '\/api\/mattermost'\);   \/\/ a failed test answers 4xx/);
  assert.match(mm, /export function startPresence\(\): void \{/);   // the presence heartbeat is unchanged (DRUHY-PRUCHOD: Zachovat)
  assert.match(css, /\/\* ==== 9\.(?:XX|\d+) F22 [^\n]*==== \*\/[\s\S]*\.mm-status \{[\s\S]*\/\* ==== end F22 ==== \*\//);
});
