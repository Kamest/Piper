'use strict';
const { updatesEnabled, updateDecision } = require('./policy.cjs');

// The newest desktop release, published by the Pages workflow: a CDN file without GitHub's anonymous API limit,
// which a shared (company VPN) address exhausts. The API list is only the fallback.
const MANIFEST = 'https://kamest.github.io/Piper/desktop/latest.json';
const RELEASES = 'https://api.github.com/repos/Kamest/Piper/releases';
const versionParts = value => /^\d+\.\d+\.\d+$/.test(value || '') ? value.split('.').map(Number) : null;
const newer = (a, b) => {
  const left = versionParts(a), right = versionParts(b);
  if (!left || !right) return false;
  for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] > right[i];
  return false;
};

async function releaseList(fetch) {
  try {
    const response = await fetch(MANIFEST, { signal: AbortSignal.timeout(10000) });
    if (response.ok) {
      const latest = await response.json();
      if (latest && typeof latest.tag_name === 'string' && typeof latest.html_url === 'string')
        return [{ tag_name: latest.tag_name, html_url: latest.html_url }];
    }
  } catch { /* the API below */ }
  const response = await fetch(RELEASES, { signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw Object.assign(new Error('Release check failed'), { reason: 'HTTP ' + Number(response.status) });
  const releases = await response.json();
  if (!Array.isArray(releases)) throw new Error('Invalid release list');
  return releases;
}

/**
 * Why a check or download failed, safe for Copy Details: an error's name and system code, never its message (it can
 * carry paths, hosts or tokens), so About itself still shows only the generic text.
 */
function failureReason(error) {
  const safe = value => typeof value === 'string' && /^[\w .:-]{1,60}$/.test(value) ? value : '';
  if (safe(error?.reason)) return error.reason;
  const code = safe(error?.code) || safe(error?.cause?.code), name = safe(error?.name);
  return [name, code].filter(Boolean).join(' ') || 'unknown';
}

/**
 * Desktop updates never operate on piperd or its sessions. All effects are explicit for tests.
 * Linux installs by itself only as an AppImage (`appImage`); a deb package opens the release page.
 */
function desktopUpdates({ version, platform, arch, packaged, windowsStore, signed, appImage = false, fetch, updater, openExternal,
  changed = () => {}, now = () => new Date().toISOString() }) {
  let state = { current: version, latest: '', status: windowsStore ? 'store' : packaged ? 'idle' : 'development',
    method: (platform === 'darwin' && !signed) || (platform === 'linux' && !appImage) ? 'download' : 'install-on-quit', progress: null,
    checkedAt: null, releaseURL: '' };
  let checking, downloading, client;
  // A failing listener (tray, About) must never turn a finished check into "Couldn't check for updates".
  const set = patch => { state = { ...state, ...patch }; try { changed({ ...state }); } catch { /* the listener's own problem */ } };
  const failed = (stage, error) => set({ status: 'error', errorStage: typeof stage === 'string' ? stage : state.status === 'downloading' ? 'download' : 'check',
    errorReason: failureReason(typeof stage === 'string' ? error : stage), progress: null });
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
      set({ status: 'checking', progress: null, errorReason: '' });
      checking = (async () => {
        try {
          const releases = await releaseList(fetch);
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
            if (!result || result.updateInfo?.version !== state.latest) throw Object.assign(new Error('Desktop feed mismatch'), { reason: 'feed mismatch' });
          }
          set({ status: 'available', checkedAt: now() });
        } catch (error) { failed('check', error); }
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
        } catch (error) { failed('download', error); }
      })().finally(() => { downloading = null; });
      return downloading;
    },
    install() { if (state.status === 'ready') autoUpdater().quitAndInstall(false, true); }
  };
}
module.exports = { desktopUpdates, failureReason, MANIFEST, RELEASES };
