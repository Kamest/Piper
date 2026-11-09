'use strict';
// Windows CI only, on the throwaway runner (the OS test sandbox does not exist on Windows): the app's own
// wsl.exe argument shapes against a real WSL. `wsl.exe -- cmd` went through the default Linux shell, which
// ate the backslashes of Windows paths and re-interpreted quotes; `--exec` passes them as data.
const { execFileSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { wsl } = require('../src/lib/platform.cjs');
const distro = process.argv[2] || 'Ubuntu';
const run = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8' });
const check = (what, got, want) => {
  if (got !== want) { console.error(`${what}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`); process.exitCode = 1; }
  else console.log(`ok ${what}`);
};
const value = String.raw`C:\Program Files\Piper's "app" $HOME;echo x \\ end`;
check('helper keeps an argument as data', run(...wsl('printf', ['%s', value], distro)), value);
check('helper passes env values as data', run(...wsl('sh', ['-c', 'printf "%s" "$PIPER_X"'], distro, { PIPER_X: value })), value);
check('wslpath of a Windows path', run('wsl.exe', ['-d', distro, '--exec', 'wslpath', '-u', String.raw`C:\Windows\Temp`]).trim(), '/mnt/c/Windows/Temp');
// Reveal and native drag need the opposite conversion, in the distro selected by the
// desktop controller. A real Linux filesystem file proves the resulting UNC path is usable
// by Windows; /mnt/c alone would also pass with an accidentally selected default distro.
const payload = 'Native drag fixture: Linux to Windows\n';
const dir = run('wsl.exe', ['-d', distro, '--exec', 'mktemp', '-d', '/tmp/piper-native-files.XXXXXXXX']).trim();
if (!/^\/tmp\/piper-native-files\.[A-Za-z0-9]+$/.test(dir)) throw Error('Unexpected fixture directory');
try {
  const file = dir + "/a file's $value; fixture.txt";
  run('wsl.exe', ['-d', distro, '--exec', 'sh', '-c', 'printf "%s" "$2" > "$1"', 'fixture', file, payload]);
  const converted = run('wsl.exe', ['-d', distro, '--exec', 'wslpath', '-w', file]).trim();
  check('Linux file converts to an absolute Windows path', path.win32.isAbsolute(converted), true);
  check('Linux file conversion preserves the selected distro', converted.toLowerCase().startsWith('\\\\wsl.localhost\\' + distro.toLowerCase() + '\\') || converted.toLowerCase().startsWith('\\\\wsl$\\' + distro.toLowerCase() + '\\'), true);
  check('Windows reads the exact Linux fixture bytes', readFileSync(converted, 'utf8'), payload);
} finally {
  run('wsl.exe', ['-d', distro, '--exec', 'rm', '-rf', '--', dir]);
}
