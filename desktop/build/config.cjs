'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { forArch, missingRuntimes } = require('./arch.cjs');
const config = forArch(require('../package.json').build, process.env.PIPER_DESKTOP_ARCH);
const missing = missingRuntimes(config, process.platform, path.resolve(__dirname, '..'), fs.existsSync);
if (missing.length) throw new Error(`Bundled Python missing: ${missing.join(', ')}. Run build/python.mjs or set PIPER_DESKTOP_ARCH.`);
const signedMac = !!process.env.CSC_LINK || !!process.env.CSC_NAME;
module.exports = {
  ...config,
  extraMetadata: { desktopSigned: signedMac },
  ...(process.env.ELECTRON_CACHE ? { electronDownload: { cache: process.env.ELECTRON_CACHE } } : {}),
  mac: { ...config.mac, ...(signedMac ? {} : { identity: null }) },
  win: { ...config.win, signAndEditExecutable: !!(process.env.WIN_CSC_LINK || process.env.CSC_LINK) }
};
