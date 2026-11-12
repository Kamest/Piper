'use strict';
const api = window.piperAbout, root = document.getElementById('about');
let previous = '', first = true, copyTimer;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
const button = (name, label, primary = false, disabled = false) => '<button type="button" data-action="' + name + '"' + (primary ? ' class="primary"' : '') + (disabled ? ' disabled' : '') + '>' + esc(label) + '</button>';
function updateView(u, mac = true) {
  const version = u.latest, notes = button('notes', 'Release Notes');
  switch (u.status) {
    case 'checking': return ['running', '…', 'Checking for updates…', 'Looking for a newer Piper Desktop release.', button('check', 'Checking…', false, true)];
    case 'current': return ['ok', '✓', 'Piper Desktop is up to date', u.current + ' is the latest version · checked ' + (u.checkedAt ? new Date(u.checkedAt).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }) : 'just now'), button('check', 'Check Again')];
    case 'available': return ['warn', '↓', 'Piper Desktop ' + version + ' is available', u.method === 'download' ? (mac ? 'This build updates manually: download it, quit Piper Desktop and replace the app in Applications.' : 'This package updates manually: download the new package from the release page and install it.') : 'Download now; it installs when Piper Desktop quits.', button('download', 'Download ' + version, true) + notes];
    case 'downloading': return ['running', '↓', 'Downloading Piper Desktop ' + version + '…', (u.progress === null ? '' : Math.round(u.progress) + ' % · ') + 'You can keep working; nothing restarts yet.', notes];
    case 'ready': return ['ok', '✓', 'Piper Desktop ' + version + ' is ready', 'It installs when Piper Desktop quits. Sessions keep running in the Piper service.', button('install', 'Restart to Update', true) + button('close', 'Later')];
    case 'error': return ['problem', '!', u.errorStage === 'download' ? 'Couldn’t download Piper Desktop ' + version : 'Couldn’t check for updates', u.errorStage === 'download' ? 'The download didn’t finish. Check your connection and try again.' : 'The release server could not be reached. Check your connection and try again.', button('retry', 'Try Again', true) + button('releases', 'Open Releases')];
    case 'store': return ['', '✓', 'Updates come from the Microsoft Store', 'Windows installs new versions of Piper Desktop through the Store.', button('store', 'Open Microsoft Store')];
    case 'development': return ['', '–', 'Updates are off in development builds', 'Install a released build to receive updates.', ''];
    default: return ['', '↑', 'Check for updates', 'Not checked yet', button('check', 'Check for Updates', true)];
  }
}
function render(s) {
  const key = JSON.stringify(s); if (key === previous) return; previous = key;
  const action = document.activeElement?.closest('[data-action]')?.dataset.action;
  const focusInside = root.contains(document.activeElement);
  const u = s.update, [tone, mark, title, detail, actions] = updateView(u, !/^(Linux|Windows)/.test(s.system || ''));
  const serviceDetail = s.online ? s.serviceLocation + ' · version ' + (s.serviceVersion || 'not reported') : 'Not reachable. Desktop details and updates still work.';
  const serviceNote = s.online && !s.serviceVersion ? 'Update the Piper service to see its version. It updates separately from the desktop app.' : 'The service is updated separately from the desktop app.';
  const channel = u.status === 'store' ? 'Microsoft Store' : u.status === 'development' ? 'Development' : u.method === 'download' ? 'Manual download' : 'Automatic';
  root.innerHTML =
    '<div class="about-header"><img class="about-icon" src="icon.png" alt=""><div class="about-identity"><h1 id="app-name">Piper Desktop</h1><div class="detail">Version ' + esc(s.version) + ' · ' + esc(s.system) + ' · ' + esc(s.architecture) + '</div></div>' + button('copy', 'Copy Details') + '</div>' +
    '<section class="about-section" aria-labelledby="desktop-label"><div class="about-section-head"><span class="about-label" id="desktop-label">Desktop app</span><span class="tag">' + channel + '</span></div><div class="about-status about-update"><span class="mark ' + tone + '" aria-hidden="true">' + mark + '</span><div class="about-text"><div role="status" aria-live="polite" aria-atomic="true" aria-busy="' + (u.status === 'checking') + '"><div class="about-status-title ' + (u.status === 'error' ? 'bad' : '') + '">' + esc(title) + '</div><div class="detail">' + esc(detail) + '</div></div>' +
    (u.status === 'downloading' ? '<progress class="progress" aria-label="Desktop update download" max="100"' + (u.progress === null ? '' : ' value="' + u.progress + '"') + '></progress>' : '') +
    '<div class="about-actions">' + actions + '</div></div></div></section>' +
    '<section class="about-section" aria-labelledby="service-label"><div class="about-section-head"><span class="about-label" id="service-label">Piper service</span><span class="tag ' + (s.online ? 'ok' : 'bad') + '">' + (s.online ? 'Running' : 'Not reachable') + '</span></div><div class="about-status"><span class="mark ' + (s.online ? 'ok' : 'problem') + '" aria-hidden="true">' + (s.online ? '✓' : '!') + '</span><div class="about-text"><div class="about-status-title">' + esc(serviceDetail) + '</div><div class="detail">' + esc(serviceNote) + '</div>' + (!s.online ? '<div class="about-actions">' + button('setup', 'Open Setup') + '</div>' : '') + '</div></div></section>' +
    '<nav class="about-links" aria-label="Project links">' + button('project', 'Project on GitHub') + notesButton() + button('issue', 'Report an Issue') + '</nav>' +
    '<div class="about-footer"><span class="hint">Open source · Apache-2.0</span>' + button('close', 'Close') + '</div><span class="about-copy-status" role="status" aria-live="polite"></span>';
  if (first || focusInside) {
    const same = [...root.querySelectorAll('[data-action]')].find(b => b.dataset.action === action && !b.disabled);
    (same || (action === 'download' ? root.querySelector('[data-action="notes"]') : null) || root.querySelector('button.primary:not(:disabled)') || root.querySelector('[data-action="close"]')).focus();
  }
  first = false;
  requestAnimationFrame(() => api.resize(38 + root.scrollHeight));
}
function notesButton() { return button('notes', 'Release Notes'); }
root.addEventListener('click', event => {
  const target = event.target.closest('[data-action]'); if (!target || target.disabled) return;
  void api.action(target.dataset.action).then(() => {
    if (target.dataset.action !== 'copy') return;
    const copy = root.querySelector('[data-action="copy"]'); if (copy) copy.textContent = 'Copied';
    root.querySelector('.about-copy-status').textContent = 'Copied';
    clearTimeout(copyTimer); copyTimer = setTimeout(() => { const b = root.querySelector('[data-action="copy"]'); if (b) b.textContent = 'Copy Details'; }, 1500);
  });
});
document.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); void api.action('close'); } });
api.subscribe(render);
void api.getState().then(render);
