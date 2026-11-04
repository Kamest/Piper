import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Exercise the registered production callback, including its tray refresh after the window update.
const source = readFileSync(new URL('../../desktop/src/main.cjs', import.meta.url), 'utf8');
const handler = source.match(/nativeTheme\.on\('updated', (\(\) => \{[^\n]+\})\);/)[1];
const tokens = { page: '#111111', card: '#222222', fg: '#eeeeee' };
function update(local) {
  let refreshed = 0;
  vm.runInNewContext('(' + handler + ')()', { local, theme: () => tokens, refreshTray: () => refreshed++ });
  return refreshed;
}

test('system theme updates a macOS local window without the Windows/Linux overlay API', () => {
  for (const hidden of [false, true]) {
    let background;
    const local = { isDestroyed: () => false, isVisible: () => !hidden, setBackgroundColor: color => background = color };
    assert.equal(update(local), 1);
    assert.equal(background, tokens.page);
  }
});

test('system theme still updates supported title bar overlays', () => {
  let background, overlay;
  assert.equal(update({ isDestroyed: () => false, setBackgroundColor: color => background = color,
    setTitleBarOverlay: options => overlay = options }), 1);
  assert.equal(background, tokens.page);
  assert.deepEqual(JSON.parse(JSON.stringify(overlay)), { color: tokens.card, symbolColor: tokens.fg, height: 38 });
});

test('system theme refreshes the tray without a local window or after its destruction', () => {
  assert.equal(update(undefined), 1);
  assert.equal(update({ isDestroyed: () => true,
    setBackgroundColor: () => assert.fail('destroyed window was updated'),
    setTitleBarOverlay: () => assert.fail('destroyed window overlay was updated') }), 1);
});
