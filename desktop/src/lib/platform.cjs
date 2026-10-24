'use strict';
const path = require('node:path');

const quote = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
function wsl(command, args, distro, env = {}) {
  const values = Object.entries(env).filter(([k]) => /^[A-Z_][A-Z0-9_]*$/.test(k)).map(([k, v]) => `${k}=${quote(v)}`).join(' ');
  return ['wsl.exe', ['-d', distro, '--', 'sh', '-c', `exec env ${values} ${[command, ...args].map(quote).join(' ')}`]];
}
function installArgs(env, extension) {
  const args = ['--yes', '--no-open', '--service', ...(extension ? ['--extension', extension] : ['--no-extensions'])];
  if (env.PROJECT_PATH) args.push('--dir', path.posix.join(env.PROJECT_PATH, 'piper'));
  return args;
}
function wslRows(text) {
  return String(text).replace(/\0/g, '').split(/\r?\n/).flatMap(line => {
    const m = /^\s*\*?\s*(\S.*?)\s{2,}\S+\s+(\d)\s*$/.exec(line);
    return m ? [{ name: m[1].trim(), version: Number(m[2]) }] : [];
  });
}
function wslBootstrap(executable) {
  // RunOnce resumes only the window; wsl.exe performs no reboot itself.
  const ps = s => "'" + s.replace(/'/g, "''") + "'";
  return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    `$ErrorActionPreference='Stop'; New-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce' -Name PiperSetup -Value ${ps('"' + executable + '"')} -PropertyType String -Force | Out-Null; $p=Start-Process wsl.exe -ArgumentList '--install --no-distribution' -Verb RunAs -Wait -PassThru; if ($p.ExitCode -notin @(0,3010)) {exit $p.ExitCode}`]];
}
function podmanCommand(platform, { distro = 'Ubuntu', manager, packagePath } = {}) {
  if (platform === 'darwin') {
    if (!packagePath) throw new Error('A verified Podman package is required');
    const script = `do shell script ${JSON.stringify('/usr/sbin/installer -pkg ' + quote(packagePath) + ' -target /')} with administrator privileges`;
    return ['osascript', ['-e', script]];
  }
  if (platform === 'win32') return ['wsl.exe', ['-d', distro, '-u', 'root', '--', 'sh', '-c', 'apt-get update && apt-get install -y podman uidmap']];
  const managers = { apt: ['apt-get', 'install', '-y', 'podman', 'uidmap'], dnf: ['dnf', 'install', '-y', 'podman'], pacman: ['pacman', '-S', '--needed', '--noconfirm', 'podman'], zypper: ['zypper', '--non-interactive', 'install', 'podman'] };
  if (!managers[manager]) throw new Error('Install Podman with your distribution package manager, then check again.');
  return ['pkexec', managers[manager]];
}
function linuxAutostart(executable) {
  if (/[\r\n\0]/.test(executable)) throw new Error('Invalid executable path');
  return `[Desktop Entry]\nType=Application\nName=Piper\nExec="${executable.replace(/[\\"`$]/g, '\\$&').replace(/%/g, '%%')}"\nTerminal=false\nX-GNOME-Autostart-enabled=true\n`;
}
module.exports = { quote, wsl, wslRows, wslBootstrap, podmanCommand, installArgs, linuxAutostart };
