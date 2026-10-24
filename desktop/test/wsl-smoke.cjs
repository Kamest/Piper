'use strict';
// Windows CI only, on the throwaway runner (the OS test sandbox does not exist on Windows): the app's own
// wsl.exe argument shapes against a real WSL. `wsl.exe -- cmd` went through the default Linux shell, which
// ate the backslashes of Windows paths and re-interpreted quotes; `--exec` passes them as data.
const { execFileSync } = require('node:child_process');
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
