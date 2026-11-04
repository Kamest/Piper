'use strict';
const { updatesEnabled, updateDecision } = require('./policy.cjs');

const RELEASES = 'https://api.github.com/repos/Kamest/Piper/releases';
const versionParts = value => /^\d+\.\d+\.\d+$/.test(value || '') ? value.split('.').map(Number) : null;
const newer = (a, b) => {
  const left = versionParts(a), right = versionParts(b);
  if (!left || !right) return false;
  for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] > right[i];
  return false;
};

/** Desktop updates never operate on piperd or its sessions. All effects are explicit for tests. */
function desktopUpdates({ version, platform, arch, packaged, windowsStore, signed, fetch, updater, openExternal,
  changed = () => {}, now = () => new Date().toISOString() }) {
  let state = { current: version, latest: '', status: windowsStore ? 'store' : packaged ? 'idle' : 'development',
    method: platform === 'darwin' && !signed ? 'download' : 'install-on-quit', progress: null,
    checkedAt: null, releaseURL: '' };
  let checking, downloading, client;
  const set = patch => { state = { ...state, ...patch }; changed({ ...state }); };
  const failed = stage => set({ status: 'error', errorStage: typeof stage === 'string' ? stage : state.status === 'downloading' ? 'download' : 'check', progress: null });
  function autoUpdater() {
    if (client) return client;
    client = updater();
    client.logger = null; client.autoDownload = false; client.autoInstallOnAppQuit = true; client.allowDowngrade = false;
    client.on('error', failed);
    client.on('download-progress', progress => {
      if (state.status !== 'downloading') return;
      const percent = Number(progress.percent);
      set({ status: 'downloading', progress: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null });
    });
    client.on('update-downloaded', () => { if (state.status === 'downloading') set({ status: 'ready', progress: 100 }); });
    return client;
  }
  return {
    state: () => ({ ...state }),
    check() {
      if (checking) return checking;
      if (!updatesEnabled(packaged, windowsStore) || ['downloading', 'ready'].includes(state.status)) return Promise.resolve();
      set({ status: 'checking', progress: null });
      checking = (async () => {
        try {
          const response = await fetch(RELEASES, { signal: AbortSignal.timeout(10000) });
          if (!response.ok) throw new Error('Release check failed');
          const releases = await response.json();
          if (!Array.isArray(releases)) throw new Error('Invalid release list');
          let release;
          for (const candidate of releases) {
            if (!candidate || candidate.draft || candidate.prerelease ||
                updateDecision(platform, signed, candidate.tag_name, candidate.html_url) === 'ignore') continue;
            const next = candidate.tag_name.slice('desktop-v'.length);
            if (newer(next, version) && (!release || newer(next, release.tag_name.slice('desktop-v'.length)))) release = candidate;
          }
          if (!release) { set({ status: 'current', latest: '', releaseURL: '', checkedAt: now() }); return; }
          set({ latest: release.tag_name.slice('desktop-v'.length), releaseURL: release.html_url });
          if (state.method === 'install-on-quit') {
            const appUpdater = autoUpdater();
            appUpdater.channel = 'desktop-' + arch;
            appUpdater.setFeedURL({ provider: 'generic', url: 'https://github.com/Kamest/Piper/releases/download/' + release.tag_name + '/', channel: 'desktop-' + arch });
            const result = await appUpdater.checkForUpdates();
            if (state.status === 'error') return;
            // A feed inconsistent with the chosen public release must never announce a usable update.
            if (!result || result.updateInfo?.version !== state.latest) throw new Error('Desktop feed mismatch');
          }
          set({ status: 'available', checkedAt: now() });
        } catch { failed(); }
      })().finally(() => { checking = null; });
      return checking;
    },
    download() {
      if (downloading) return downloading;
      if (state.status !== 'available' && !(state.status === 'error' && state.errorStage === 'download')) return Promise.resolve();
      downloading = (async () => {
        try {
          if (state.method === 'download') { await openExternal(state.releaseURL); return; }
          set({ status: 'downloading', progress: 0 });
          await autoUpdater().downloadUpdate();
        } catch { failed('download'); }
      })().finally(() => { downloading = null; });
      return downloading;
    },
    install() { if (state.status === 'ready') autoUpdater().quitAndInstall(false, true); }
  };
}
module.exports = { desktopUpdates };
