'use strict';
const api = window.piperDesktop;
const root = document.getElementById('root');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const button = (text, action, primary = false, disabled = false, value = '') => `<button data-action="${action}" data-value="${escape(value)}" class="${primary ? 'primary' : ''}" ${disabled ? 'disabled' : ''}>${text}</button>`;
const mark = status => `<span class="mark ${status}">${({ ok: '✓', install: '+', running: '…', problem: '!' })[status] || '·'}</span>`;

function render(s) {
  const e = s.consent?.[s.index];
  const footer = (hint, actions) => `<footer><span class="hint">${hint}</span>${actions}</footer>`;
  let body = '', step = ['check', 'runtime', 'wsl'].includes(s.screen) ? 0 : s.screen === 'extension' ? 1 : 2;
  if (s.screen === 'check') {
    body = `<h1>Checking what Piper needs</h1><p>Nothing is installed yet. Piper looks first, then shows you what it will add.</p><div class="checks">${s.rows.map(r => `<div class="check">${mark(r.status)}<div class="text"><div>${escape(r.name)}</div><div class="detail">${escape(r.detail)}</div></div>${r.status === 'install' ? '<span class="detail">will install</span>' : r.status === 'problem' && r.action ? button('Install git', r.action) : ''}</div>`).join('')}</div>${footer('Nothing changes until you press Continue.', button('Quit','quit') + button('Continue','continue',true,s.checking || s.busy || s.rows.some(r => r.status === 'problem' && !['docker','wsl'].includes(r.id))))}`;
  } else if (s.screen === 'extension' && e) {
    body = `<h1>An extension was found</h1><p>A checkout on this computer adds features to Piper. Load it only if you know where it comes from: it runs on this computer, outside the sandbox.</p><section class="extension"><div class="extension-head"><div class="avatar">${escape(e.label?.[0] || 'E')}</div><div class="text"><strong>${escape(e.label)}</strong><div class="detail">${e.source === 'registered' ? 'registered by its checkout' : 'found by scanning'} · extension api ${escape(e.api)}</div></div><span class="tag ok">compatible</span></div><dl>${[['Folder',e.path],['From',e.remote || 'No remote'],['Commit',e.commit || 'Unknown'],['Adds',e.description || 'Features provided by this extension']].map(([k,v]) => `<div><dt>${k}</dt><dd class="${k === 'Folder' || k === 'Commit' ? 'mono' : ''}">${escape(v)}</dd></div>`).join('')}</dl></section>${[['load','Load it','Piper starts through the extension from now on. You can turn it off in Settings › Extensions.'],['skip','Not now','Piper runs alone. The doctor keeps offering it.']].map(([v,title,hint]) => `<label class="choice"><input type="radio" name="choice" value="${v}" ${s.choices[e.path] === v ? 'checked' : ''}><span><strong>${title}</strong><span class="detail">${hint}</span></span></label>`).join('')}${footer(`Found ${s.extensions.length} extension${s.extensions.length === 1 ? '' : 's'}.`,button('Back','back')+button('Continue','next',true,s.busy))}`;
  } else if (s.screen === 'install') {
    body = `<h1>Installing</h1><p>About two minutes. You can close the window — setup goes on and Piper opens when it is done.</p><div class="checks">${['hole · the sandbox','Piper','Piper service','Doctor'].map((name,i) => `<div class="check">${mark(i < (s.phase || 0) ? 'ok' : i === (s.phase || 0) ? 'running' : '')}<div class="text">${name}</div><span class="detail">${i < (s.phase || 0) ? 'done' : i === (s.phase || 0) ? 'installing' : 'waiting'}</span></div>`).join('')}</div><progress class="progress" max="4" value="${(s.phase || 0) + 1}"></progress><pre>${escape(s.log.slice(-5).join('\n'))}</pre>${footer('Log saved in the app’s setup.log',button('Show full log','log'))}`;
  } else if (s.screen === 'runtime') {
    body = `<h1>Piper needs Docker</h1><p>Sessions run in containers. No running Docker or Podman was found on this computer.</p>${[
      ['Start Docker','If Docker Desktop is installed, start it, then press Check again.','Check again','check',''],
      ['Install Docker yourself','Docker Desktop, OrbStack or Colima — whatever your team uses. Piper finds it next time.','Open guide','guide',''],
      ['Let Piper install Podman','Only if you have nothing else. Free and open source. Asks for your password once.','Install Podman','podman','last']
    ].map(([title,hint,label,action,cls]) => `<section class="option ${cls}"><div class="text"><strong>${title}${cls ? '<span class="tag">last resort</span>' : ''}</strong><p>${hint}</p></div>${button(label,action,false,s.busy)}</section>`).join('')}${footer('Check the requirements again after starting a runtime.',button('Quit','quit'))}`;
  } else if (s.screen === 'wsl') {
    body = `<h1>Turn on WSL, then restart</h1><p>On Windows Piper runs inside WSL 2, the Linux built into Windows. Turning it on needs administrator rights and one restart. After the restart Piper continues by itself.</p><ol class="wsl-steps"><li>Windows asks for permission (one prompt)</li><li>WSL and Ubuntu are installed</li><li>You restart the computer</li><li>Piper opens and finishes the setup inside Ubuntu</li></ol>${s.message ? `<p>${escape(s.message)}</p>` : ''}${footer('Docker Desktop with WSL integration is found later.',button('Quit','quit')+button('Turn on WSL','wsl',true,s.busy))}`;
  } else if (s.screen === 'error') {
    body = `<h1>Setup stopped</h1><div class="error"><strong>${escape(s.error)}</strong><p>Nothing was removed. Already installed components stay installed; the next try resumes setup.</p></div><pre>${escape(s.log.slice(-6).join('\n'))}</pre><p class="detail">Check the log, fix the failed prerequisite, then try again.</p>${footer('',button('Copy log','copy')+button('Open in browser','browser')+button('Try again','retry',true,s.busy))}`;
  } else if (s.screen === 'offline') {
    root.innerHTML = `<main class="offline"><span class="mark">!</span><h1>Piper is not running</h1><p>The window lost the Piper service at 127.0.0.1:${escape(s.port)}. Your sessions keep running in their sandboxes; the service only needs to start again.</p><div class="buttons">${button('Show log','log')+button('Start Piper','ensure',true,s.busy)}</div><div class="detail">${escape(s.error || 'Retrying every 5 s …')}</div></main>`; return;
  } else if (s.screen === 'extensions') {
    root.innerHTML = `<main class="extensions"><h1>Extensions</h1><p>Found on this computer. Piper runs through a loaded extension.</p>${s.extensions.map(e => `<section class="extrow"><div class="text"><strong>${escape(e.label)}</strong><span class="tag ${e.compatible ? e.trusted ? 'ok' : '' : 'bad'}">${!e.compatible ? 'incompatible' : e.trusted ? 'loaded' : 'found'}</span><div class="detail mono">${escape(e.path)}</div></div>${!e.compatible ? '' : button(e.trusted ? 'Turn off' : 'Load…',e.trusted ? 'untrust' : 'load',false,s.busy,e.path)}</section>`).join('') || '<p>No extensions found.</p>'}<p class="note">Turning one on or off restarts the Piper service. Sessions keep running.</p></main>`; return;
  }
  root.innerHTML = `<div class="wizard"><aside class="rail"><div class="eyebrow">SET UP PIPER</div><ol>${['Check this computer','Extensions','Install','Ready'].map((name,i) => `<li class="${i === step ? 'current' : ''}"><span class="num">${i < step ? '✓' : i + 1}</span>${name}</li>`).join('')}</ol></aside><main class="content">${body}</main></div>`;
}
root.addEventListener('click', event => {
  const b = event.target.closest('[data-action]');
  if (b && !b.disabled) void api.action(b.dataset.action, b.dataset.value || undefined);
});
root.addEventListener('change', event => { if (event.target.name === 'choice') void api.action('choice', event.target.value); });
api.subscribe(render);
void api.getState().then(render);
