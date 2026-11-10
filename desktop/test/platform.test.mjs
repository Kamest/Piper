import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { wsl, wslRows, wslBootstrap, podmanCommand, installArgs, linuxAutostart, linuxLauncher, integrateAppImage } = require('../src/lib/platform.cjs');

test('WSL selects only version 2 and tolerates UTF-16 output', () => {
  const text = '  NAME            STATE           VERSION\r\n* Ubuntu          Running         2\r\n  Debian          Stopped         1';
  assert.deepEqual(wslRows(text.split('').join('\0')), [{name:'Ubuntu',version:2},{name:'Debian',version:1}]);
});
test('WSL preserves spaces, quotes and shell metacharacters as data', () => {
  const [cmd,args] = wsl('sh',["/home/test/a ' b.sh",'--extension','/tmp/$(danger)'],'Ubuntu',{PIPER_HOME:'/home/test/state space',PIPER_PORT:'18995'});
  assert.equal(cmd,'wsl.exe'); assert.deepEqual(args.slice(0,5),['-d','Ubuntu','--exec','sh','-c']);
  assert.ok(args.at(-1).includes("'/tmp/$(danger)'")); assert.ok(args.at(-1).includes("'\\''"));
});
test('WSL bootstrap uses UAC and a per-user one-shot resume, never forces reboot', () => {
  const [cmd,args] = wslBootstrap('C:\\Program Files\\Piper\\Piper.exe');
  assert.equal(cmd,'powershell.exe'); assert.match(args.at(-1),/RunOnce/); assert.match(args.at(-1),/-Verb RunAs/); assert.match(args.at(-1),/--no-distribution/); assert.doesNotMatch(args.at(-1),/Restart-Computer|shutdown/);
});
test('Podman commands are explicitly platform-specific and require known package managers', () => {
  assert.equal(podmanCommand('darwin',{packagePath:'/tmp/podman.pkg'})[0],'osascript');
  assert.throws(()=>podmanCommand('darwin'));
  assert.deepEqual(podmanCommand('linux',{manager:'apt'}),['pkexec',['apt-get','install','-y','podman','uidmap']]);
  assert.throws(()=>podmanCommand('linux',{manager:'unknown'}));
  assert.deepEqual(podmanCommand('win32',{distro:'Ubuntu'})[1].slice(0,6),['-d','Ubuntu','-u','root','--exec','sh']);
});
test('installer receives explicit consent, service flags and a neutral project checkout', () => {
  assert.deepEqual(installArgs({PROJECT_PATH:'/work/projects'},'/work/team/ext'),['--yes','--no-open','--service','--extension','/work/team/ext','--dir','/work/projects/piper']);
  assert.deepEqual(installArgs({},null),['--yes','--no-open','--service','--no-extensions']);
});
test('Linux autostart quotes the executable and rejects line injection', () => {
  assert.match(linuxAutostart('/opt/Piper App/Piper'),/Exec="\/opt\/Piper App\/Piper"/);
  assert.throws(()=>linuxAutostart('/tmp/a\nHidden=true'));
});
test('Linux entries carry the icon and window class a panel needs to match a pinned window', () => {
  const opts = { icon: '/home/u/.local/share/icons/hicolor/512x512/apps/piper-desktop.png', wmClass: 'Piper' };
  for (const entry of [linuxAutostart('/opt/a/Piper.AppImage', opts), linuxLauncher('/opt/a/Piper.AppImage', opts)]) {
    assert.match(entry, /^Icon=\/home\/u\/.local\/share\/icons\/hicolor\/512x512\/apps\/piper-desktop\.png$/m);
    assert.match(entry, /^StartupWMClass=Piper$/m);
  }
  assert.match(linuxLauncher('/opt/50%/$x', opts), /^Exec="\/opt\/50%%\/\\\$x"$/m);
  assert.doesNotMatch(linuxLauncher('/opt/a', opts), /no-sandbox|\.mount_/);
  assert.throws(() => linuxLauncher('/opt/a', { icon: 'x\nExec=evil' }));
});
test('AppImage registers a stable launcher and icon, follows a moved image and keeps autostart in step', async () => {
  const fs = await import('node:fs/promises');
  const os = await import('node:os');
  const path = await import('node:path');
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'piper-appimage-'));
  try {
    const iconSource = path.join(root, 'icon.png');
    await fs.writeFile(iconSource, Buffer.from([137, 80, 78, 71]));
    const dataHome = path.join(root, 'data'), configHome = path.join(root, 'config');
    const base = { iconSource, dataHome, configHome, wmClass: 'Piper', fs };
    const launcher = path.join(dataHome, 'applications/piper-desktop.desktop');
    const autostart = path.join(configHome, 'autostart/piper-desktop.desktop');
    const first = await integrateAppImage({ ...base, appImage: '/opt/piper/Piper-1.AppImage' });
    assert.equal(first.icon, path.join(dataHome, 'icons/hicolor/512x512/apps/piper-desktop.png'));
    assert.deepEqual(await fs.readFile(first.icon), await fs.readFile(iconSource));
    const entry = await fs.readFile(launcher, 'utf8');
    assert.match(entry, /^Exec="\/opt\/piper\/Piper-1\.AppImage"$/m);
    assert.match(entry, new RegExp('^Icon=' + first.icon.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$', 'm'));
    assert.equal(first.changed.length, 2, 'no autostart entry is created unless the user enabled it');
    await assert.rejects(fs.access(autostart));
    // an unchanged start writes nothing
    assert.deepEqual((await integrateAppImage({ ...base, appImage: '/opt/piper/Piper-1.AppImage' })).changed, []);
    // an updated (renamed) AppImage with autostart enabled rewrites both entries
    await fs.mkdir(path.dirname(autostart), { recursive: true });
    await fs.writeFile(autostart, linuxAutostart('/opt/piper/Piper-1.AppImage'));
    const moved = await integrateAppImage({ ...base, appImage: '/opt/piper/Piper-2.AppImage' });
    assert.deepEqual(moved.changed, [launcher, autostart]);
    assert.match(await fs.readFile(launcher, 'utf8'), /Piper-2\.AppImage/);
    const auto = await fs.readFile(autostart, 'utf8');
    assert.match(auto, /^Exec="\/opt\/piper\/Piper-2\.AppImage"$/m);
    assert.match(auto, /^StartupWMClass=Piper$/m);
    assert.match(auto, /^X-GNOME-Autostart-enabled=true$/m);
    assert.deepEqual((await fs.readdir(path.dirname(launcher))), ['piper-desktop.desktop'], 'no temporary files remain');
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
