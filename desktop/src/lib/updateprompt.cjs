'use strict';
/**
 * The update prompt in the tray: what the desktop app offers by itself. Pure, so tests decide every case.
 * - the desktop app: a background check (startup, then every CHECK_EVERY) announces a new version once per
 *   version and app run, and the tray's top item leads to the About window (download, then Restart to Update);
 * - the Piper service: /api/state's `core_update` names the version `piper update` would bring. A checkout
 *   pinned to a release (detached HEAD, e.g. an extension's wrapper) keeps its item, disabled, with the reason.
 */
const CHECK_EVERY = 6 * 60 * 60 * 1000;
const FIRST_CHECK = 30 * 1000;

/** the tray's top items for a desktop update ([] when there is none) */
function desktopItems(update) {
  if (update?.status === 'available' && update.latest) return [{ action: 'download', label: 'Update Piper Desktop to ' + update.latest + '…' }];
  if (update?.status === 'ready' && update.latest) return [{ action: 'install', label: 'Restart to Update Piper Desktop ' + update.latest }];
  return [];
}

/** the notification for a newly available desktop version, or null; `seen` holds the versions announced */
function desktopNotice(update, seen) {
  if (update?.status !== 'available' || !update.latest || seen.has(update.latest)) return null;
  seen.add(update.latest);
  return { title: 'Piper Desktop ' + update.latest + ' is available',
    body: update.method === 'download' ? 'Download it from the release page. Sessions keep running.'
      : 'Update now; it installs when Piper Desktop restarts. Sessions keep running.' };
}

/** Machine › Update Piper: its label and whether it can run */
function coreItem(core) {
  if (core && core.updatable === false && ['pinned', 'embedded'].includes(core.reason)) return { label: 'Update Piper (version set by its installer)', enabled: false };
  if (core?.available) return { label: 'Update Piper to ' + core.available, enabled: true };
  return { label: 'Update Piper', enabled: true };
}

module.exports = { CHECK_EVERY, FIRST_CHECK, desktopItems, desktopNotice, coreItem };
