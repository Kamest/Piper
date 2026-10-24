'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { forArch, missingRuntimes } = require('./arch.cjs');
const base = require('../package.json').build;
const store = process.env.PIPER_DESKTOP_STORE === '1';
const config = forArch(store ? { ...base, win: { ...base.win, target: [{ target: 'appx', arch: ['x64', 'arm64'] }] } } : base, process.env.PIPER_DESKTOP_ARCH);
const missing = missingRuntimes(config, process.platform, path.resolve(__dirname, '..'), fs.existsSync);
if (missing.length) throw new Error(`Bundled Python missing: ${missing.join(', ')}. Run build/python.mjs or set PIPER_DESKTOP_ARCH.`);
const signedMac = !!process.env.CSC_LINK || !!process.env.CSC_NAME;
const signPath = !store && process.env.PIPER_SIGNPATH === '1';
module.exports = {
  ...config,
  extraMetadata: { desktopSigned: signedMac },
  ...(store ? {
    directories: { ...config.directories, output: 'out/store', buildResources: 'resources' },
    appx: {
      identityName: process.env.PIPER_STORE_IDENTITY_NAME || 'Piper.Desktop.CI',
      publisher: process.env.PIPER_STORE_PUBLISHER || 'CN=Piper CI',
      publisherDisplayName: process.env.PIPER_STORE_PUBLISHER_DISPLAY_NAME || 'Piper contributors',
      displayName: 'Piper', applicationId: 'Piper',
      artifactName: 'Piper-${version}-win-${arch}.${ext}',
      capabilities: ['runFullTrust'], addAutoLaunchExtension: false
    },
    beforePack: context => {
      if (context.electronPlatformName !== 'win32') throw new Error('Build Store packages on Windows');
      require('node:child_process').execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-File',
        path.join(__dirname, 'store-assets.ps1'), '-Source', path.resolve(__dirname, '../resources/icon.png'),
        '-Destination', path.resolve(__dirname, '../resources/appx')], { stdio: 'inherit' });
    }
  } : {}),
  ...(process.env.ELECTRON_CACHE ? { electronDownload: { cache: process.env.ELECTRON_CACHE } } : {}),
  mac: { ...config.mac, ...(signedMac ? {} : { identity: '-', hardenedRuntime: false }) },
  win: { ...config.win, signAndEditExecutable: !!(process.env.WIN_CSC_LINK || process.env.CSC_LINK),
    ...(store ? { signAndEditExecutable: true, signExecutable: false } : {}),
    ...(signPath ? { signAndEditExecutable: true, signExecutable: false,
      signtoolOptions: { publisherName: 'SignPath Foundation' } } : {}) }
};
