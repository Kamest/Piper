'use strict';
const path = require('node:path');

const quote = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
function wsl(command, args, distro, env = {}) {
  const values = Object.entries(env).filter(([k]) => /^[A-Z_][A-Z0-9_]*$/.test(k)).map(([k, v]) => `${k}=${quote(v)}`).join(' ');
  return ['wsl.exe', ['-d', distro, '--exec', 'sh', '-c', `exec env ${values} ${[command, ...args].map(quote).join(' ')}`]];
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
function wslBootstrap(executable, windowsStore = false) {
  // Store apps resume from userData on their next manual launch. Their exe is not a launch entry point.
  const ps = s => "'" + s.replace(/'/g, "''") + "'";
  const resume = windowsStore ? '' : `New-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce' -Name PiperSetup -Value ${ps('"' + executable + '"')} -PropertyType String -Force | Out-Null; `;
  return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    `$ErrorActionPreference='Stop'; ${resume}$p=Start-Process wsl.exe -ArgumentList '--install --no-distribution' -Verb RunAs -Wait -PassThru; if ($p.ExitCode -notin @(0,3010)) {exit $p.ExitCode}`]];
}
function podmanCommand(platform, { distro = 'Ubuntu', manager, packagePath } = {}) {
  if (platform === 'darwin') {
    if (!packagePath) throw new Error('A verified Podman package is required');
    const script = `do shell script ${JSON.stringify('/usr/sbin/installer -pkg ' + quote(packagePath) + ' -target /')} with administrator privileges`;
    return ['osascript', ['-e', script]];
  }
  if (platform === 'win32') return ['wsl.exe', ['-d', distro, '-u', 'root', '--exec', 'sh', '-c', 'apt-get update && apt-get install -y podman uidmap']];
  const managers = { apt: ['apt-get', 'install', '-y', 'podman', 'uidmap'], dnf: ['dnf', 'install', '-y', 'podman'], pacman: ['pacman', '-S', '--needed', '--noconfirm', 'podman'], zypper: ['zypper', '--non-interactive', 'install', 'podman'] };
  if (!managers[manager]) throw new Error('Install Podman with your distribution package manager, then check again.');
  return ['pkexec', managers[manager]];
}
// Linux desktop entries share one name with the AppImage's embedded entry and the deb package, so Electron's
// Wayland app_id (package.json desktopName) and the X11 WM_CLASS both match the launcher a panel pins.
const LINUX_ENTRY = 'piper-desktop';
function desktopEntry(executable, { icon, wmClass } = {}, extra = '') {
  for (const value of [executable, icon, wmClass]) if (value != null && /[\r\n\0]/.test(value)) throw new Error('Invalid desktop entry value');
  return `[Desktop Entry]\nType=Application\nName=Piper\nExec="${executable.replace(/[\\"`$]/g, '\\$&').replace(/%/g, '%%')}"\n`
    + (icon ? `Icon=${icon}\n` : '') + (wmClass ? `StartupWMClass=${wmClass}\n` : '') + `Terminal=false\n${extra}`;
}
const linuxAutostart = (executable, opts) => desktopEntry(executable, opts, 'X-GNOME-Autostart-enabled=true\n');
const linuxLauncher = (executable, opts) => desktopEntry(executable, opts, 'Comment=Agent sessions, approvals and review\nCategories=Development;\nStartupNotify=true\n');
/**
 * An AppImage installs nothing into the system: register its launcher and icon for the current user, so a pinned
 * window resolves to "Piper" instead of the executable inside the temporary /tmp/.mount_* directory. Runs on every
 * start, so the entries follow a moved or updated AppImage; an autostart entry the user enabled is kept in step.
 * Writes only changed files, atomically, so an unchanged launch does not trigger a desktop database rebuild.
 */
async function integrateAppImage({ appImage, iconSource, dataHome, configHome, wmClass, fs }) {
  const icon = path.join(dataHome, 'icons/hicolor/512x512/apps', LINUX_ENTRY + '.png');
  const write = async (file, data, mode) => {
    try { if (Buffer.from(data).equals(await fs.readFile(file))) return false; } catch { /* new file */ }
    await fs.mkdir(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    await fs.writeFile(tmp, data, { mode });
    await fs.rename(tmp, file);
    return true;
  };
  const changed = [];
  if (await write(icon, await fs.readFile(iconSource), 0o644)) changed.push(icon);
  const launcher = path.join(dataHome, 'applications', LINUX_ENTRY + '.desktop');
  if (await write(launcher, linuxLauncher(appImage, { icon, wmClass }), 0o644)) changed.push(launcher);
  const autostart = path.join(configHome, 'autostart', LINUX_ENTRY + '.desktop');
  let enabled = true;
  try { await fs.access(autostart); } catch { enabled = false; }
  if (enabled && await write(autostart, linuxAutostart(appImage, { icon, wmClass }), 0o600)) changed.push(autostart);
  return { icon, changed };
}
module.exports = { quote, wsl, wslRows, wslBootstrap, podmanCommand, installArgs, linuxAutostart, linuxLauncher, integrateAppImage, LINUX_ENTRY };
